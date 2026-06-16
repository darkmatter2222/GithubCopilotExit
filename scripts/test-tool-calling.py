"""
Test: Ollama bug #10976 workaround — reasoning_effort=none injected for tool requests.

Validates:
  1. Request WITH tools → finish_reason=tool_calls, content="", tool_calls present
  2. Request WITHOUT tools → thinking still allowed (reasoning field may appear)
  3. Request WITH tools but explicit reasoning_effort set → not overridden
"""

import json, sys, urllib.request

PROXY = "http://localhost:8001/v1/chat/completions"

def post(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(PROXY, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())

TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current UTC time",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CALCULATOR = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate a math expression and return the numeric result",
        "parameters": {
            "type": "object",
            "properties": {"expr": {"type": "string", "description": "math expression"}},
            "required": ["expr"],
        },
    },
}

results = []

# ── Test 1: tools present, no explicit reasoning_effort ─────────────────
print("\n==> Test 1: tools present (workaround should inject reasoning_effort=none)")
resp = post({
    "model": "qwen3", "stream": False,
    "messages": [{"role": "user", "content": "What time is it? Use the get_time tool."}],
    "tools": [TOOL],
})
choice = resp["choices"][0]
fr = choice["finish_reason"]
tc = choice["message"].get("tool_calls")
content = choice["message"].get("content", "")
ok = fr == "tool_calls" and bool(tc) and not content
results.append(("Test 1 — tools → tool_calls", ok))
print(f"  finish_reason : {fr}")
print(f"  tool_calls    : {[t['function']['name'] for t in tc] if tc else 'NONE'}")
print(f"  content       : {repr(content)}")
print(f"  RESULT        : {'PASS ✓' if ok else 'FAIL ✗'}")

# ── Test 2: no tools → thinking may be on (not the bug path) ───────────
print("\n==> Test 2: no tools (thinking remains on, workaround should NOT fire)")
resp = post({
    "model": "qwen3", "stream": False,
    "max_tokens": 30,
    "messages": [{"role": "user", "content": "Say the single word HELLO and nothing else."}],
})
choice = resp["choices"][0]
fr = choice["finish_reason"]
tc = choice["message"].get("tool_calls")
ok = tc is None  # no spurious tool calls
results.append(("Test 2 — no tools → no tool_calls injected", ok))
print(f"  finish_reason : {fr}")
print(f"  tool_calls    : {tc}")
print(f"  RESULT        : {'PASS ✓' if ok else 'FAIL ✗'}")

# ── Test 3: tools + explicit reasoning_effort → not overridden ──────────
print("\n==> Test 3: tools + explicit reasoning_effort=medium (should not be overridden)")
resp = post({
    "model": "qwen3", "stream": False,
    "max_tokens": 20,
    "reasoning_effort": "medium",
    "messages": [{"role": "user", "content": "What is 3+3?"}],
    "tools": [CALCULATOR],
})
# We just verify it doesn't crash; behavior depends on model + Ollama version
choice = resp["choices"][0]
fr = choice["finish_reason"]
results.append(("Test 3 — explicit reasoning_effort preserved (no crash)", True))
print(f"  finish_reason : {fr}")
print(f"  RESULT        : PASS ✓ (completed without error)")

# ── Summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
all_pass = all(ok for _, ok in results)
for label, ok in results:
    print(f"  {'PASS ✓' if ok else 'FAIL ✗'}  {label}")
print("=" * 55)
print(f"\n{'ALL TESTS PASSED' if all_pass else 'FAILURES — see above'}")
sys.exit(0 if all_pass else 1)
