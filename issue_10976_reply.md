# Additional Reproduction + Full Code-Level Root Cause Analysis

Reproducing this consistently in a production VS Code Copilot agentic session using a local Ollama proxy. Adding a full code-level trace and a proposed minimal fix because the existing comments haven't pinned the exact failure path in the codebase.

---

## Environment

| Field | Value |
|---|---|
| **Ollama version** | 0.30.6 (confirmed via `/api/version`) |
| **Model** | `qwen3:30b-a3b` (Q4_K_M GGUF, 262 144-token context alias) |
| **GPU** | NVIDIA RTX 5090 (32 GB VRAM) |
| **OS** | Windows 11 (Ollama running natively) |
| **Client** | VS Code Copilot Chat via OpenAI-compatible `/v1/chat/completions` |
| **Transport** | Local FastAPI proxy → `http://localhost:11434/v1/chat/completions` |

---

## Reproduction Steps

### Minimal curl reproducer (native `/api/chat`)

```bash
curl -s http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:30b-a3b",
    "stream": false,
    "think": true,
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "read_file",
          "description": "Read a file from the workspace",
          "parameters": {
            "type": "object",
            "properties": {
              "path": { "type": "string" }
            },
            "required": ["path"]
          }
        }
      }
    ],
    "messages": [
      { "role": "user", "content": "Read the file main.py and summarize it." }
    ]
  }' | python -m json.tool
```

**Result (broken):**
```json
{
  "model": "qwen3:30b-a3b",
  "created_at": "2026-06-15T20:16:34.000Z",
  "message": {
    "role": "assistant",
    "content": "",
    "thinking": "The user wants me to read main.py. I should call the read_file tool with path='main.py'. Let me do that now."
  },
  "done_reason": "stop",
  "done": true,
  "eval_count": 892,
  "eval_duration": 2284000000,
  "prompt_eval_count": 247,
  "prompt_eval_duration": 312000000
}
```

`eval_count: 892` — the model generated 892 tokens, **all of them in the `<think>` block**. The `content` field is empty. No tool call was emitted.

---

### Same request with `think: false`

```bash
curl -s http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:30b-a3b",
    "stream": false,
    "think": false,
    "tools": [ ... same tools as above ... ],
    "messages": [
      { "role": "user", "content": "Read the file main.py and summarize it." }
    ]
  }' | python -m json.tool
```

**Result (working):**
```json
{
  "model": "qwen3:30b-a3b",
  "message": {
    "role": "assistant",
    "content": "",
    "tool_calls": [
      {
        "function": {
          "name": "read_file",
          "arguments": { "path": "main.py" }
        }
      }
    ]
  },
  "done_reason": "stop",
  "done": true
}
```

✅ Tool call correctly emitted. `eval_count` ~28 tokens.

---

### OpenAI endpoint (`/v1/chat/completions`) — what VS Code Copilot sees

VS Code Copilot always sends tool schemas and never sends `reasoning_effort`. The exact SSE stream received by the client:

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"","reasoning":"The user wants me to read main.py..."},"finish_reason":null}]}

... (892 identical chunks with content:"" and reasoning:"<token>") ...

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

**Every `choices[0].delta.content` value is `""`.**  
Copilot receives zero content tokens → shows "No response returned."

Proxy log confirming this:
```
2026-06-15 20:16:32,564 INFO HTTP Request: POST http://localhost:11434/v1/chat/completions "HTTP/1.1 200 OK"
2026-06-15 20:16:34,850 INFO [req-0] completed — 0 tokens generated
```
`0 tokens generated` = our proxy counted zero chunks where `choices[0].delta.content` was non-empty. The model ran for 2.3 seconds but produced no usable content.

---

## Root Cause — Traced Through the Codebase

### Step 1: `/v1/chat/completions` arrives → `openai/openai.go: FromChatRequest()`

```go
// openai/openai.go  (current main branch)
func FromChatRequest(r ChatCompletionRequest, ...) (*api.ChatRequest, error) {
    // ...
    var think *api.ThinkValue
    if r.ReasoningEffort != "" {
        effort := strings.ToLower(r.ReasoningEffort)
        // ...
        if effort == "none" {
            think = &api.ThinkValue{Value: false}
        } else {
            think = &api.ThinkValue{Value: effort}
        }
    }
    return &api.ChatRequest{
        // ...
        Think: think,   // ← nil when no reasoning_effort was sent
    }, nil
}
```

**VS Code Copilot never sends `reasoning_effort`.** Therefore `think = nil` enters `ChatHandler`.

---

### Step 2: `server/routes.go: ChatHandler()` — thinking forced on

```go
// server/routes.go — ChatHandler
caps := []model.Capability{model.CapabilityCompletion}
if len(req.Tools) > 0 {
    caps = append(caps, model.CapabilityTools)
}
modelCaps := m.Capabilities()
if slices.Contains(modelCaps, model.CapabilityThinking) {
    caps = append(caps, model.CapabilityThinking)
    if req.Think == nil {
        req.Think = &api.ThinkValue{Value: true}   // ← FORCED ON when nil
    }
}
```

Because `qwen3` has `CapabilityThinking`, **`req.Think` is unconditionally set to `true`** when the client didn't explicitly set it. This is the first critical point: a client that sent no thinking preference gets thinking forced on.

---

### Step 3: `routes.go: ChatHandler()` — tool parser and thinking parser created simultaneously

```go
// Both parsers are created at the same time:

// Thinking parser (built from <think>/<\think> tags in the model template)
var thinkingState *thinking.Parser
openingTag, closingTag := thinking.InferTags(m.Template.Template)
if req.Think != nil && req.Think.Bool() && openingTag != "" && closingTag != "" {
    thinkingState = &thinking.Parser{
        OpeningTag: openingTag,
        ClosingTag: closingTag,
    }
}

// Tool parser
var toolParser *tools.Parser
if len(req.Tools) > 0 && (builtinParser == nil || !builtinParser.HasToolSupport()) {
    toolParser = tools.NewParser(m.Template.Template, req.Tools)
}
```

Both `thinkingState` and `toolParser` are non-nil. They are **independent** — neither knows about the other.

---

### Step 4: `routes.go: ChatHandler()` — the streaming completion callback (the actual bug)

```go
// Simplified from the streaming goroutine in ChatHandler
err := r.Completion(ctx, llm.CompletionRequest{...}, func(r llm.CompletionResponse) {
    res := api.ChatResponse{
        Message: api.Message{Role: "assistant", Content: r.Content},
        // ...
    }

    // --- THINKING PARSER ---
    if thinkingState != nil {
        thinkingContent, remainingContent := thinkingState.AddContent(res.Message.Content)
        res.Message.Thinking = thinkingContent
        res.Message.Content = remainingContent   // ← empty string after </think>
    }

    // --- TOOL PARSER (runs AFTER thinking parser) ---
    if len(req.Tools) > 0 {
        toolCalls, content := toolParser.Add(res.Message.Content)  // ← receives ""
        if len(content) > 0 {
            res.Message.Content = content
        } else if len(toolCalls) > 0 {
            res.Message.ToolCalls = toolCalls
            res.Message.Content = ""
        } else if res.Message.Thinking != "" {
            // don't return, fall through to ch <- res
        } else {
            // buffer while tool call accumulates
            if r.Done {
                res.Message.Content = toolParser.Content()
                ch <- res
            }
            return  // ← swallowed — not sent to client
        }
    }

    ch <- res   // ← sent with Content:"" and ToolCalls:nil
})
```

**The exact failure sequence when the model generates only a `<think>` block:**

| Token stream from llama-server | After `thinkingState.AddContent()` | After `toolParser.Add()` | Sent to channel? |
|---|---|---|---|
| `<think>` | Thinking: `<think>`, Content: `""` | No tool calls, no content | Yes (thinking update) |
| `I should call read_file...` | Thinking: `...`, Content: `""` | No tool calls, no content, `Thinking != ""` | Yes (fall-through) |
| `</think>` | Thinking: `</think>`, Content: `""` | No tool calls, no content | Yes |
| `\n` (stop) | Thinking: `""`, Content: `""` | No tool calls, no content, `Thinking == ""` → buffering path | **Swallowed** |
| `[done=true]` | — | `toolParser.Content()` = `""` | Yes, but content is `""` |

**Net result: `choices[0].delta.content = ""` on every single SSE chunk.**

The model did produce a tool call *intent* in its reasoning, but it expressed that intent only inside `<think>` and then stopped generating — it never emitted the `<tool_call>` tokens that the tool parser is looking for. With thinking OFF, the model immediately emits `<tool_call>{"name":"read_file",...}</tool_call>` tokens that the tool parser correctly extracts.

---

### Step 5: `openai/openai.go: ToChunks()` — confirms empty content sent to client

```go
func ToChunks(id string, r api.ChatResponse, toolCallSent bool) []ChatCompletionChunk {
    hasMixedResponse := r.Message.Thinking != "" && (r.Message.Content != "" || len(r.Message.ToolCalls) > 0)
    if !hasMixedResponse {
        return []ChatCompletionChunk{toChunk(id, r, toolCallSent)}
    }
    // ... split into reasoning chunk + content chunk
}
```

When `Thinking != ""` but `Content == ""` and `ToolCalls == nil` (exactly our case), `hasMixedResponse = false`. A single chunk is returned with `Content: ""`. No special handling, no error, just silence.

---

## Why It Gets Worse in Long Agentic Loops

In a VS Code Copilot agentic session, every request includes the full conversation history: prior tool calls, tool results, assistant responses. As context grows:

1. **Attention budget**: The model devotes more of its generation capacity to processing the long context in `<think>`, leaving less "energy" for actual content generation.
2. **Template interaction**: The Qwen3 Jinja chat template, when it sees tool definitions in the system/tools section AND is in thinking mode, appears to condition the model toward expressing tool call intent exclusively in the `<think>` section rather than emitting `<tool_call>` tokens.
3. **KV cache pressure**: At ~120K+ tokens, the KV cache fills on a 32 GB GPU, causing additional context shifts that further disrupt generation quality.

The proxy log pattern that confirms this:
```
# Short early-session request (small context)
2026-06-15 20:14:11 INFO [req-5] completed — 47 tokens generated   ✅

# Long mid-session request (large accumulated context + tools)
2026-06-15 20:16:34 INFO [req-12] completed — 0 tokens generated   ❌
```

---

## Why `reasoning_effort: "none"` Is the Only Reliable Workaround

From `openai.go`:
```go
if effort == "none" {
    think = &api.ThinkValue{Value: false}
}
```

This is the **only path** that sets `Think = false` through the `/v1/chat/completions` endpoint. When `Think = false`, the thinking parser is never created, the model emits tool call tokens directly, and the tool parser extracts them correctly.

Other attempted workarounds and why they fail:

| Attempt | Why it fails |
|---|---|
| `chat_template_kwargs: {enable_thinking: false}` | Silently ignored in Ollama 0.30.6 (see #10809) |
| `options: {think: false}` | `think` is not an `api.Options` field, ignored |
| Sending `think: false` in request body | Only works via `/api/chat`, not `/v1/chat/completions` |
| `max_tokens` (low value) | Truncates thinking block, model never reaches content generation |

---

## Proposed Fix

The simplest fix with minimal blast radius: in `ChatHandler`, when **both** thinking capability and tools are requested, and the client did not explicitly request thinking (`req.Think == nil`), **default to `think=false` instead of `think=true`**:

```go
// server/routes.go — ChatHandler (proposed change)
if slices.Contains(modelCaps, model.CapabilityThinking) {
    caps = append(caps, model.CapabilityThinking)
    if req.Think == nil {
-       req.Think = &api.ThinkValue{Value: true}
+       // When tools are also present, thinking ON by default produces empty output.
+       // Only enable thinking automatically when no tools are requested.
+       if len(req.Tools) > 0 {
+           req.Think = &api.ThinkValue{Value: false}
+       } else {
+           req.Think = &api.ThinkValue{Value: true}
+       }
    }
}
```

Alternatively, the `thinking` parser and `tool` parser need to be made aware of each other. When the model generates only a `<think>` block with no subsequent `<tool_call>` tokens, the pipeline should either:
- Re-prompt without thinking to force content generation, OR
- Fall back to treating the thinking block's final intent as the tool call (higher complexity, model-specific)

The simple approach above (disable thinking when tools are present and client didn't opt in) matches the behavior that actually works and is the same mitigation that third-party projects have independently adopted (see [openlegion-ai/openlegion#491](https://github.com/openlegion-ai/openlegion/commit/04b84c5f445a35470b3d33e2e825ee107a565efc)).

---

## Related Issues

- #10976 — this issue (original)
- #10929 — linked by @rick-github
- #14493 — Qwen 3.5 27B tool calling non-functional  
- #14601 — Qwen3 tool calling malformed definitions
- #15288 — Gemma4: same symptom via `/v1` (different root cause)

---

## Summary

The failure is **not** a model weight issue and **not** a template rendering issue per se. It is a **policy conflict** in `ChatHandler`:

1. `FromChatRequest` correctly leaves `Think=nil` when the OpenAI client says nothing about thinking.
2. `ChatHandler` unconditionally promotes `Think=nil` → `Think=true` for any model with `CapabilityThinking`.
3. With `Think=true` + `tools` present simultaneously, the Qwen3 model puts all output into `<think>` tokens and produces no `<tool_call>` tokens.
4. The thinking parser strips the thinking content, leaving `Content=""` for the tool parser.
5. The tool parser buffers empty input, finds no tool calls, and emits empty SSE chunks.
6. The client receives `choices[0].delta.content=""` on every chunk — effectively a silent empty response.

The fix is to not auto-promote thinking to `true` when tools are also in the request.
