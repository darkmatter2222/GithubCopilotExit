import urllib.request, json, time

def test_thinking(url, model, extra=None):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Tell me a one-sentence story."}],
        "max_tokens": 80,
        "stream": False
    }
    if extra:
        body.update(extra)
    data = json.dumps(body).encode()
    t = time.time()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    elapsed = time.time() - t
    c = d["choices"][0]
    content = c["message"].get("content", "") or ""
    reasoning = c["message"].get("reasoning_content", "") or ""
    print(f"time: {elapsed:.2f}s | finish: {c.get('finish_reason')}")
    print(f"  content ({len(content)} chars): {repr(content[:80])}")
    print(f"  reasoning ({len(reasoning)} chars): {repr(reasoning[:60])}")

print("=== Direct coder :8002 — reasoning_effort=none only ===")
test_thinking("http://localhost:8002/v1/chat/completions", "qwen3-coder",
              {"reasoning_effort": "none"})

print("\n=== Direct coder :8002 — chat_template_kwargs only ===")
test_thinking("http://localhost:8002/v1/chat/completions", "qwen3-coder",
              {"chat_template_kwargs": {"enable_thinking": False}})

print("\n=== Direct coder :8002 — both ===")
test_thinking("http://localhost:8002/v1/chat/completions", "qwen3-coder",
              {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}})

print("\n=== Proxy coder :8001/coder — no extra (proxy injects) ===")
test_thinking("http://localhost:8001/v1/chat/completions/coder", "qwen3-coder")
