# Copilot-Local — Run Your Own LLM on a GPU Server

> Ditch API costs and run Qwen3 locally on your own GPU hardware. A production-ready stack with live monitoring, built for VS Code Copilot but works with anything that speaks the OpenAI API.

## Architecture at a Glance

```
  ┌──────────────────┐        HTTP :8001       ┌─────────────────────┐     :11434     ┌──────────────────┐
  │   YOUR LAPTOP    ├────────────────────────►│  GPU SERVER         ├───────────────►│ Docker: ollama     │
  │                  │                         │                     │                │ (Ollama v0.30.8)    │
  │  VS Code         │                         │ Docker: llm-proxy   │                │                    │
  │  Copilot Chat   ◄├─────────────────────────┤  + uvicorn          │                │ qwen3 alias        │
  │                  │    streamed SSE response │  (FastAPI proxy)     │                │ 27B · Q4_K_M       │
  └──────────────────┘                         │                     │                │ 262K context         │
                                               │ ─────────────────── │                │ RTX GPU ≥ 24GB VRAM│
                                               │ • Clamp temp ≥ 0.6  │                └──────────────────┘
                                               │ • Rewrite model name│
                                               │ • Track token TPS   │
  ┌──────────────────┐                         │ • Pass tools/vision │
  │   YOUR BROWSER    ├────────────────────────►• /dashboard (live)   │
  │                  │    HTTP GET              • /stats (JSON API)   │
  │ http://<IP>:     │                               ┌────────┐      │
  │   8001/dashboard ◄───────────────────────────────│ live   │      │
  └──────────────────┘                              │ stats  │      │
                                                    └────────┘      │
                                               └─────────────────────┘
```

### Component Guide

| Piece | What It Does | Where it runs |
|---|---|---|
| **VS Code Copilot** | Sends chat completions via configured custom endpoint | Your laptop |
| **LLM Proxy** (FastAPI + uvicorn) | Intercepts requests, clamps temperature ≥ 0.6 (Qwen3 requires it), rewrites model names, tracks token throughput in real time | GPU server (Docker container) |
| **Ollama** | Inference engine — loads GGUF weights onto VRAM, generates tokens, streams SSE response | GPU server (Docker container) |
| **qwen3.6 27B** | The model itself: 27B parameters, Q4_K_M quantized (~18 GB VRAM), native tool calling + vision, 262K context | GPU server (VRAM) |
| **Live Dashboard** | Auto-refreshing HTML page at /dashboard showing tokens/sec, active requests, per-request stats | Any browser on your LAN |

### Proxy Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat (what VS Code talks to) |
| `POST /v1/completions` | Raw completions |
| `GET  /health` | Health check — proxies to Ollama status |
| `GET  /dashboard` | **LIVE dashboard** — open in browser, auto-refreshes every 2s |

---

## Why This Exists

VS Code Copilot sends a temperature of 0.1 by default. Qwen3 thinking mode requires temperature ≥ 0.6 or the model produces garbage. The proxy lives between VS Code and Ollama on your GPU server — it clamps the temperature, rewrites model names to match what you deployed, and gives you a live monitoring dashboard so you can see real-time token throughput when multiple clients hit the model simultaneously.

**In short:** It makes Qwen3 work correctly in VS Code Copilot with zero config changes on the IDE side.

---

## Quick Start

### What You Need

1. A Linux machine (bare metal or VM) with an NVIDIA GPU (**24 GB+ VRAM recommended**)
2. [Docker](https://docs.docker.com/get-docker/) + [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on that server
3. SSH access from your laptop to the server

### 1-Command Deploy

```bash
git clone https://github.com/yourname/copilot-local.git
cd copilot-local
cp .env.example .env          # Fill in your remote server IP, username, key path
bash scripts/deploy-remote.sh
```

The script:
1. Uploads proxy source code to the GPU server
2. Builds a Docker image for the FastAPI proxy
3. Pulls the qwen3 model into Ollama via `docker exec`
4. Starts both Docker containers with health checks
5. Runs smoke tests against the stack

### Verify Everything Works

```bash
curl http://YOUR_SERVER_IP:8001/health
# → {"status":"ok","ollama":true} ✅
```

Open **http://YOUR_SERVER_IP:8001/dashboard** in a browser to watch live token throughput.

---

## VS Code Setup

Point VS Code at the proxy using `chatLanguageModels.json` (in `%APPDATA%\Code\User\` on Windows):

```jsonc
{
  "name": "Local GPU",
  "vendor": "customendpoint",
  "apiKey": "no-key",
  "apiType": "chat-completions",
  "models": [{
    "id": "qwen3",
    "name": "Qwen3.6-27B (Local GPU)",
    "url": "http://<YOUR_SERVER_IP>:8001/v1/chat/completions",
    "toolCalling": true,
    "vision": true,
    "maxInputTokens": 120000,
    "maxOutputTokens": 16000,
    "thinking": true,
    "streaming": true
  }]
}
```

**Important:** Use your GPU server's LAN IP (e.g., `192.168.86.48`), not `localhost`. If you run uvicorn locally with the proxy code, `localhost` works too.

---

## Live Stats Dashboard

Open **`http://<SERVER_IP>:8001/dashboard`** in any browser. The page auto-refreshes every 2 seconds and shows:

- **Combined tokens/sec** — total throughput across ALL active requests
- **Active requests** — how many clients are streaming right now
- **Total tokens this session** — cumulative count since proxy last restarted
- **Per-request table** — individual TPS, token count, live/done status

The dashboard is backed by an in-memory token tracker (`tracker.py`) that intercepts every SSE delta with non-empty content and counts it. No external dependencies. Stats persist only for the lifetime of the proxy process (no database).

---

## Project Structure

```
copilot-local/
├── proxy/
│   ├── Dockerfile           # Container image for FastAPI proxy
│   ├── main.py              # Proxy app — routes, temp clamping, streaming
│   ├── requirements.txt     # Python deps (fastapi, uvicorn, httpx)
│   └── tracker.py           # Real-time token throughput tracker
├── scripts/
│   ├── deploy-remote.sh     # One-command deploy to GPU server
│   ├── fix-context.sh       # Fix Ollama: restart with 262K context + recreate qwen3 alias
│   ├── start-proxy-local.ps1 # Run proxy locally (Windows dev)
│   ├── test-proxy.py        # Smoke test script
│   └── warmup.py            # Load model into VRAM after startup
├── .env.example             # Template — copy to .env and fill in
├── .gitignore
└── AGENTS.md                # Stack state reference for AI agents
```

---

## Known Issues & Fixes

| Error | Root Cause | Fix |
|---|---|---|
| `ERR_INCOMPLETE_CHUNKED_ENCODING` at exactly 5 min | httpx timeout was 300s | Fixed — `timeout=None` in proxy |
| `finish_reason: length` / response truncated | qwen3 alias has 32K context instead of 262K | Run `scripts/fix-context.sh` on remote |
| `unknown model architecture: 'qwen35'` | Wrong Ollama Docker image | Use `v0.30.8-final` tag specifically |
| `llama-server binary not found` | `:updated` custom image broke inference | Use `v0.30.8-final` |
| Model cold on first request (slow response) | `OLLAMA_KEEP_ALIVE` not set or container restarted | Set `-e OLLAMA_KEEP_ALIVE=-1`, run `warmup.py` |

---

## Recovery Runbook

If the stack on your GPU server breaks and needs a full restore:

```bash
# SSH in
ssh -i $SSH_KEY_PATH $SSH_USER@$SSH_HOST

# Full fix: restart Ollama, recreate alias, warm VRAM
bash scripts/fix-context.sh           # or scp it over first from this repo

# Rebuild and restart the proxy
bash scripts/deploy-remote.sh

# Verify health
curl http://localhost:8001/health
# → {"status": "ok", "ollama": true}

# Verify context is 262K (not 32K!)
curl -s http://localhost:11434/api/ps
# Look for: "context_length": 262144
```

---

## License & Contributing

Open to PRs. This was built out of frustration with API costs and the desire to leverage an existing GPU purchase locally. Share it far and wide — local AI should be easy.