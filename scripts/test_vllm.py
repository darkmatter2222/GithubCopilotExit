import urllib.request, json, time

def test(url, model, label):
    data = json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with only: vLLM works!"}], "max_tokens": 30}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.load(r)
            ttft = time.time() - t0
            content = resp["choices"][0]["message"]["content"].strip()
            print(f"{label}: OK ({ttft:.2f}s) → {content[:50]!r}")
    except Exception as e:
        print(f"{label}: ERROR → {e}")

test("http://localhost:8001/v1/chat/completions", "qwen3", "Proxy→vLLM qwen3")
test("http://localhost:8000/v1/chat/completions", "qwen3", "Direct vLLM qwen3")
