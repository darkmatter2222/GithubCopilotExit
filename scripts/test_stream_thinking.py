import urllib.request, json, time

def test_streaming(url, model, extra=None, label=""):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Tell me a one-sentence story."}],
        "max_tokens": 80,
        "stream": True
    }
    if extra:
        body.update(extra)
    data = json.dumps(body).encode()
    t = time.time()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    content_tokens = 0
    reasoning_tokens = 0
    finish = None
    with urllib.request.urlopen(req, timeout=60) as r:
        for line in r:
            line = line.decode("utf-8").strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if delta.get("content"):
                            content_tokens += 1
                        if delta.get("reasoning_content"):
                            reasoning_tokens += 1
                        if choices[0].get("finish_reason"):
                            finish = choices[0]["finish_reason"]
                except:
                    pass
    elapsed = time.time() - t
    print(f"{label}")
    print(f"  time: {elapsed:.2f}s | finish: {finish}")
    print(f"  content chunks: {content_tokens} | reasoning chunks: {reasoning_tokens}")
    if reasoning_tokens > 0:
        print("  *** THINKING IS ACTIVE (reasoning_content chunks present) ***")

print("=== Stream: coder :8002 — reasoning_effort=none ===")
test_streaming("http://localhost:8002/v1/chat/completions", "qwen3-coder",
               {"reasoning_effort": "none"}, "direct")

print("\n=== Stream: coder :8002 — chat_template_kwargs ===")
test_streaming("http://localhost:8002/v1/chat/completions", "qwen3-coder",
               {"chat_template_kwargs": {"enable_thinking": False}}, "direct")

print("\n=== Stream: proxy coder :8001/coder (proxy injects reasoning_effort=none) ===")
test_streaming("http://localhost:8001/v1/chat/completions/coder", "qwen3-coder",
               label="via proxy")

print("\n=== Stream: qwen3 :8000 — reasoning_effort=none ===")
test_streaming("http://localhost:8000/v1/chat/completions", "qwen3",
               {"reasoning_effort": "none"}, "direct qwen3")
