# GithubCopilotExit

Run **Qwen3.6-27B** locally on an RTX GPU via Ollama with a FastAPI temperature proxy — giving you a fully private, OpenAI-compatible AI coding assistant that replaces GitHub Copilot.

> **Why this exists:** GitHub Copilot sends `temperature=0.1` by default. Qwen3's thinking mode requires `temperature >= 0.6` or the model collapses into degenerate output. This repo provides a thin proxy that clamps the temperature, pins the model in VRAM, and serves a clean OpenAI API endpoint that any IDE extension can talk to.

---

## Architecture

```
VS Code / Roo Code / Continue / Cursor
        │
        │  OpenAI-compatible API  (port 8001)
        ▼
┌─────────────────────────────┐
│   FastAPI Temperature Proxy  │  proxy/main.py
│   • clamps temp to ≥ 0.6    │  Dockerized, --network host
│   • rewrites model name     │
│   • passes tool schemas     │
│   • no auth required        │
└────────────┬────────────────┘
             │  Ollama API  (localhost:11434)
             ▼
┌─────────────────────────────┐
│   Ollama  (Docker)           │  ollama/ollama:v0.30.8-final
│   OLLAMA_KEEP_ALIVE=-1       │  model stays in VRAM forever
│   OLLAMA_NUM_CTX=262144      │  262K context safety net
│   qwen3 alias (262K ctx)     │  created via Modelfile
└────────────┬────────────────┘
             │  GPU inference
             ▼
     NVIDIA RTX 3090 (24 GB VRAM)
     Model: qwen3.6:27b-mtp-q4_K_M (~18 GB loaded)
     ~2 GB VRAM remaining for KV cache headroom
```

---

## Hardware Requirements

| Component | Minimum | This Setup |
|---|---|---|
| GPU | 24 GB VRAM | RTX 3090 24 GB |
| System RAM | 32 GB | — |
| Storage | 25 GB free | NVMe recommended |
| OS | Linux (Docker host) | Ubuntu |
| Docker | 24+ with GPU support | Docker + nvidia-container-toolkit |

---

## Model

**`qwen3.6:27b-mtp-q4_K_M`** — Qwen3.6 27B parameter, Q4_K_M quantization

| Property | Value |
|---|---|
| VRAM required | ~18 GB (model weights) + ~2 GB (262K KV cache) |
| Context window | 262,144 tokens (262K) |
| Tool calling | Native (no prompt engineering) |
| Vision | Yes (text + image input) |
| Thinking mode | Yes — requires `temperature >= 0.6` |
| Quantization | Q4_K_M (~4.93 bits/weight) |
| MTP | Multi-Token Prediction — faster generation |

**Critical:** The model is served as the `qwen3` alias (not the raw `qwen3.6:27b-mtp-q4_K_M` name) because the alias bakes in `num_ctx 262144`. Using the raw model name gives only 32K context.

---

## Quick Start — Local Windows (Ollama on Windows)

For running directly on a Windows machine with an RTX GPU:

### 1. Install Ollama

Download from https://ollama.com and install. Verify:
```powershell
ollama --version  # should be >= 0.30
```

### 2. Pull the model

```powershell
ollama pull qwen3.6:27b-mtp-q4_K_M
```

This downloads ~18 GB. First time only.

### 3. Start the server

```powershell
.\scripts\start-vllm.ps1
```

This:
- Starts Ollama on port 8000
- Creates the `qwen3` alias with 262K context
- Verifies the server is ready

### 4. Point your client at the endpoint

| Setting | Value |
|---|---|
| Base URL | `http://localhost:8000/v1` |
| Model | `qwen3` |
| API Key | `local` (any string) |

---

## Full Setup — Remote Linux Server (Recommended)

This is the production setup: Ollama + temperature proxy both run as Docker containers on a Linux machine with an RTX GPU. VS Code on Windows talks to it over the LAN.

### Prerequisites on the Linux server

```bash
# Docker with GPU support
curl -fsSL https://get.docker.com | sh
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# Verify GPU is accessible inside Docker
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

### Step 1 — Configure your .env

Copy the example and fill in your values:
```powershell
copy .env.example .env
```

Edit `.env`:
```ini
SSH_USER=your_linux_username
SSH_HOST=192.168.x.x          # your server's LAN IP
SSH_KEY_PATH=~/.ssh/id_rsa
REMOTE_PATH=/home/youruser/llm-stack
API_PORT=8001
OLLAMA_MODEL=qwen3.6:27b-mtp-q4_K_M
SERVED_MODEL_NAME=qwen3
MIN_TEMPERATURE=0.6
```

### Step 2 — Deploy everything

```bash
bash scripts/deploy-remote.sh
```

This script:
1. SSH-copies proxy source files to the remote server
2. Builds the `llm-proxy` Docker image on the remote
3. Pulls `qwen3.6:27b-mtp-q4_K_M` into Ollama
4. Starts the proxy container on port 8001
5. Runs smoke tests (model list + temperature clamp verification)

### Step 3 — Fix the Ollama container (CRITICAL — read this)

The Ollama Docker image version matters enormously. **Only `ollama/ollama:v0.30.8-final`** is known to work with `qwen3.6:27b-mtp-q4_K_M` as of June 2026.

- `ollama/ollama:latest` — too old, does not know the `qwen35` architecture, fails with `unknown model architecture`
- `ollama/ollama:updated` (custom build) — replaced CLI binary but not `llama-server`, fails with `llama-server binary not found`
- `ollama/ollama:v0.30.8-final` — ✅ works

Run this once after the server is up to:
- Restart Ollama with `OLLAMA_KEEP_ALIVE=-1` (model never evicted from VRAM)
- Restart Ollama with `OLLAMA_NUM_CTX=262144` (262K context as default)
- Create the `qwen3` alias with 262K context baked in
- Warm up the model into VRAM

```bash
bash scripts/fix-context.sh
```

You must run `fix-context.sh` **every time the Ollama container is recreated** (not just restarted — Docker volumes preserve the models, but env vars and aliases are reset on `docker rm`).

### Step 4 — Point VS Code at the endpoint

Edit `%APPDATA%\Code\User\chatLanguageModels.json`:

```json
[
  {
    "name": "Local RTX 3090",
    "vendor": "customendpoint",
    "apiKey": "no-key",
    "apiType": "chat-completions",
    "models": [
      {
        "id": "qwen3",
        "name": "Qwen3.6-27B (RTX 3090)",
        "url": "http://YOUR_SERVER_IP:8001/v1/chat/completions",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 131072,
        "maxOutputTokens": 32000,
        "thinking": true,
        "streaming": true
      }
    ]
  }
]
```

**Why `maxInputTokens: 131072` and not 262144?** With 24 GB VRAM, after the 18 GB model weights are loaded, there's ~6 GB left for the KV cache. At 262K context, the full KV cache would require more VRAM than available. 128K is the practical safe ceiling. The KV cache grows with actual input length, so short conversations work fine.

**Why `maxOutputTokens: 32000`?** Qwen3's thinking mode burns 5,000–15,000 tokens on the internal `<think>...</think>` monologue before writing output. 32K gives enough room for deep thinking + a full response without hitting the VS Code-side output cap.

---

## Known Issues and Fixes

### ERR_INCOMPLETE_CHUNKED_ENCODING (timeout at exactly 5 minutes)

**Symptom:** VS Code shows `net::ERR_INCOMPLETE_CHUNKED_ENCODING`. Log shows `networkError | qwen3 | 300107ms`.

**Cause:** The proxy was using `httpx.AsyncClient(timeout=300)`. Qwen3 on complex agentic tasks with 262K context can take 6–15 minutes to generate a full response. At exactly 300 seconds, httpx killed the connection.

**Fix:** `proxy/main.py` now uses `timeout=None` on all `httpx.AsyncClient` instances. The proxy is on localhost so there is no network-level risk. This was fixed in this repo — rebuild and redeploy the proxy if you hit this.

### finish_reason: length — Response too long

**Symptom:** VS Code throws `Response too long`. Log shows `finish_reason: [length]`.

**Cause A:** `maxOutputTokens` in `chatLanguageModels.json` is too low for thinking mode.
**Fix A:** Set `maxOutputTokens` to `32000` or higher.

**Cause B (more common):** The Ollama `qwen3` alias was not created with `num_ctx 262144`, so the model ran with the default 32K context window. Input + thinking tokens consumed the entire context, leaving zero room for output.
**Fix B:** Run `bash scripts/fix-context.sh` to recreate the alias with 262K context.

### Model not loading into VRAM (cold start latency)

**Symptom:** First request takes 20–30 seconds longer than usual. `/api/ps` returns empty.

**Cause:** `OLLAMA_KEEP_ALIVE` was not set (default 5 minutes), model was evicted from VRAM after idle time. Or the container was recreated and warmup wasn't run.

**Fix:** `OLLAMA_KEEP_ALIVE=-1` env var on the container pins the model forever. Run `scripts/warmup.py` after any container restart to load the model before VS Code's first request.

### unknown model architecture: 'qwen35'

**Symptom:** Ollama logs show `error loading model architecture: unknown model architecture: 'qwen35'`, HTTP 500 on inference.

**Cause:** The `ollama/ollama:latest` Docker image is too old to know the `qwen35` architecture family (introduced in Ollama ≥0.28).

**Fix:** Use `ollama/ollama:v0.30.8-final` specifically. Do not use `:latest` — it will pull an older cached image on your Docker host.

### llama-server binary not found

**Symptom:** Ollama logs show `llama-server binary not found`, inference fails.

**Cause:** The `ollama/ollama:updated` custom image (built by `update-ollama-remote.sh`) replaced only the `ollama` CLI binary at `/usr/local/bin/ollama` but not the `llama-server` inference engine that lives in `/usr/bin/` inside the image.

**Fix:** Use `ollama/ollama:v0.30.8-final`. Do not use the `:updated` image for inference.

---

## Scripts Reference

| Script | What it does |
|---|---|
| `scripts/start-vllm.ps1` | Start Ollama on Windows with the `qwen3` alias (262K ctx, local use) |
| `scripts/deploy-remote.sh` | Full remote deploy: proxy + Ollama model pull + smoke test |
| `scripts/fix-context.sh` | **Run this after any Ollama container rebuild.** Restarts Ollama with 262K env vars, recreates `qwen3` alias, warms up VRAM |
| `scripts/warmup.py` | Sends one chat request to load the `qwen3` model into VRAM |
| `scripts/test-proxy.py` | Smoke test: hits `/v1/models`, `/health`, and `/v1/chat/completions` with temp=0.1 to verify clamping |
| `scripts/run-vllm-wsl.sh` | Alternative: run vLLM (not Ollama) via WSL2 on Windows |
| `scripts/update-ollama-remote.sh` | **Do not use for inference.** Historical artifact — attempted to update Ollama binary inside Docker, broke llama-server |

---

## Proxy Reference (`proxy/`)

The FastAPI proxy (`proxy/main.py`) is the core of this setup.

### What it does

1. **Temperature clamping** — Any request with `temperature < 0.6` is silently raised to `0.6`. VS Code Copilot sends `0.1` by default; Qwen3 thinking mode collapses at that value.
2. **Model name rewriting** — Any model name the client sends is rewritten to `SERVED_MODEL_NAME` (default: `qwen3`). This means you can point any client at this proxy regardless of what model name it uses.
3. **Full streaming support** — SSE / chunked streaming is proxied byte-for-byte.
4. **Tool calling passthrough** — Tool schemas pass through unmodified.
5. **Azure OpenAI shims** — `/openai/deployments/{deployment}/chat/completions` maps to chat completions for clients that use Azure-style URLs.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where to find Ollama |
| `SERVED_MODEL_NAME` | `qwen3` | Model name to serve and rewrite requests to |
| `MIN_TEMPERATURE` | `0.6` | Minimum temperature floor |

### Building and deploying

```bash
# On the remote server, in the deploy directory:
docker build -t llm-proxy:latest .
docker rm -f llm-proxy
docker run -d \
  --name llm-proxy \
  --network host \
  --restart unless-stopped \
  -e OLLAMA_BASE_URL=http://localhost:11434 \
  -e SERVED_MODEL_NAME=qwen3 \
  -e MIN_TEMPERATURE=0.6 \
  llm-proxy:latest
```

---

## Files

```
GithubCopilotExit/
├── .env                    # Your secrets — gitignored, never committed
├── .env.example            # Template — copy to .env and fill in
├── .gitignore
├── AGENTS.md               # Instructions for AI coding agents operating in this repo
├── README.md               # This file
├── index.html              # Project landing page
├── proxy/
│   ├── Dockerfile          # Python 3.12-slim, uvicorn
│   ├── main.py             # FastAPI proxy — temperature clamp, model rewrite, streaming
│   └── requirements.txt    # fastapi, uvicorn, httpx
└── scripts/
    ├── deploy-remote.sh    # Full remote deployment script
    ├── fix-context.sh      # Fix Ollama after container rebuild (262K ctx + warmup)
    ├── run-vllm-wsl.sh     # Alternative: vLLM via WSL2
    ├── start-vllm.ps1      # Local Windows: start Ollama with qwen3 alias
    ├── test-proxy.py       # Smoke test the proxy endpoint
    ├── update-ollama-remote.sh  # Do not use — historical artifact
    └── warmup.py           # Load qwen3 alias into GPU VRAM
```
