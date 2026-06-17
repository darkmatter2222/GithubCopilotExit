#!/usr/bin/env python3
import urllib.request, json, sys, os

BASE = "http://localhost:11434"

# Read SERVED_MODEL_NAME from .env so this stays in sync with the proxy config
def _read_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

_read_env()
MODEL = os.environ.get("SERVED_MODEL_NAME", "gemma-coder")

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
