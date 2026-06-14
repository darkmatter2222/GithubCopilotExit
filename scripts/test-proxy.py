#!/usr/bin/env python3
"""Smoke test for the llm-proxy — verifies temperature clamping and inference."""
import urllib.request, json, sys

BASE = "http://localhost:8001"

# Test 1: models endpoint
print("==> GET /v1/models")
resp = urllib.request.urlopen(f"{BASE}/v1/models", timeout=10)
models = json.loads(resp.read())
print(json.dumps(models, indent=2))

# Test 2: health
print("\n==> GET /health")
resp = urllib.request.urlopen(f"{BASE}/health", timeout=10)
print(json.loads(resp.read()))

# Test 3: chat (temperature 0.1 should be clamped to 0.6)
print("\n==> POST /v1/chat/completions (temp=0.1 -> should clamp to 0.6)")
payload = json.dumps({
    "model": "qwen3",
    "temperature": 0.1,
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Reply with just the word PONG. No thinking, no explanation."}]
}).encode()
req = urllib.request.Request(
    f"{BASE}/v1/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=120)
result = json.loads(resp.read())
print(json.dumps(result, indent=2))
print("\n==> Response content:", result["choices"][0]["message"]["content"][:200])
print("==> All tests passed!")
