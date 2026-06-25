import urllib.request, json, time

def test(url, model, label):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Say only: works!"}], "max_tokens": 10}).encode()
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=60) as r:
            d = json.loads(r.read())
        print(f'{label}: OK ({time.time()-t:.2f}s) -> {d["choices"][0]["message"]["content"].strip()[:30]!r}')
    except Exception as e:
        print(f"{label}: FAIL - {e}")

test("http://localhost:8000/v1/chat/completions", "qwen3",       "Direct qwen3 :8000")
test("http://localhost:8002/v1/chat/completions", "qwen3-coder", "Direct coder :8002")
test("http://localhost:8001/v1/chat/completions", "qwen3",       "Proxy qwen3  :8001")
test("http://localhost:8001/v1/chat/completions/coder", "qwen3-coder", "Proxy coder  :8001/coder")
