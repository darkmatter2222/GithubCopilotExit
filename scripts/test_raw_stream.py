import urllib.request, json

body = json.dumps({
    "model": "qwen3-coder",
    "messages": [{"role": "user", "content": "Say: hello"}],
    "max_tokens": 20,
    "stream": True,
    "reasoning_effort": "none"
}).encode()

req = urllib.request.Request(
    "http://localhost:8002/v1/chat/completions",
    body, {"Content-Type": "application/json"}
)
print("=== RAW SSE STREAM FROM CODER :8002 ===")
with urllib.request.urlopen(req, timeout=30) as r:
    for line in r:
        s = line.decode("utf-8").rstrip()
        if s:
            print(repr(s))
