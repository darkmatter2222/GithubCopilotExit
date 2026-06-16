#!/usr/bin/env python3
import urllib.request, json, sys

BASE = "http://localhost:11434"
MODEL = "qwen3"  # alias with num_ctx 262144 — use this, not the raw base model

payload = json.dumps({
    "model": MODEL,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False
}).encode()

req = urllib.request.Request(
    f"{BASE}/api/chat",
    data=payload,
    headers={"Content-Type": "application/json"}
)

print(f"==> Loading {MODEL} into VRAM (may take ~15-20s)...")
try:
    r = urllib.request.urlopen(req, timeout=120)
    resp = json.loads(r.read())
    content = resp.get("message", {}).get("content", "")
    # Strip non-ASCII so Windows console doesn't choke on emoji in model responses
    safe = content.encode("ascii", errors="ignore").decode("ascii")
    print("OK -", safe[:80])
except Exception as e:
    print("ERROR:", e)
    sys.exit(1)
