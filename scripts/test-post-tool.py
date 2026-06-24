"""
Test: what does the model return after a tool result is sent back?
This simulates the second request VS Code makes after executing a tool.
"""
import httpx, json

body = {
    "model": "qwen3",
    "stream": False,
    "messages": [
        {"role": "user", "content": "Create a file called test.txt with hello world"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "create_file",
                "arguments": json.dumps({"path": "test.txt", "content": "hello world"})
            }}]
        },
        {"role": "tool", "content": "File created successfully", "tool_call_id": "call_1"}
    ],
    "tools": [{"type": "function", "function": {
        "name": "create_file",
        "description": "Create a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}
        }, "required": ["path", "content"]}
    }}]
}

print("==> Follow-up request after tool execution")
print(f"  Has 'tools' in body : {bool(body.get('tools'))}")
print(f"  Sending to proxy   : http://localhost:8001/v1/chat/completions")
print()

r = httpx.post("http://localhost:8001/v1/chat/completions", json=body, timeout=120)
resp = r.json()
choice = resp.get("choices", [{}])[0]
msg = choice.get("message", {})

print(f"  finish_reason : {choice.get('finish_reason')}")
print(f"  has tool_calls: {bool(msg.get('tool_calls'))}")
content = msg.get("content") or ""
print(f"  content len   : {len(content)} chars")
print(f"  content[:300] : {repr(content[:300])}")
print()
if content:
    print("RESULT: PASS — model produced text content after tool result ✓")
elif msg.get("tool_calls"):
    print("RESULT: OK — model made another tool call (multi-step)")
else:
    print("RESULT: FAIL — empty content after tool result (would trigger fallback message)")
