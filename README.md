# Copilot-Local — Private AI Coding Assistant on Your Own GPU

Run **Qwen3.6 27B** locally on your RTX 5090. Zero API costs. Full 262K context. Native tool calling and vision. Works exactly like GitHub Copilot but runs entirely on your hardware.

---

## How It Works

```
  VS Code Copilot Chat
        │
        │  OpenAI-compatible API  (localhost:8001)
        ▼
  ┌─────────────────────────────┐
  │   FastAPI Proxy              │  proxy/main.py  — runs on YOUR machine
  │   • Clamps temp to >= 0.6   │  (Qwen3 thinking mode needs this)
  │   • Rewrites model name     │
  │   • Tracks token TPS live   │
  │   • /dashboard (live stats) │
  └────────────┬────────────────┘
               │  localhost:11434
               ▼
  ┌─────────────────────────────┐
  │   Ollama (local)             │  ollama.com — runs on YOUR machine
  │   model: qwen3 alias         │
  │   (262K context baked in)    │
  └────────────┬────────────────┘
               │  PCIe / NVLink
               ▼
     RTX 5090  (local GPU)
     qwen3.6:27b-mtp-q4_K_M  (~18 GB VRAM)
```

Everything runs on your machine. No cloud. No subscription. No data leaves your box.

---

## Prerequisites

1. **Windows** with an RTX 5090 (or any NVIDIA GPU with 24 GB+ VRAM)
2. **Ollama** installed: https://ollama.com (just download and run the installer)
3. **Python 3.12+** installed: https://python.org
4. **VS Code** with the GitHub Copilot extension

---

## First-Time Setup (Run Once)

```powershell
git clone https://github.com/darkmatter2222/GithubCopilotExit.git
cd GithubCopilotExit

# One-time setup: installs deps, pulls model (~18 GB), creates qwen3 alias
.\scripts\setup-local.ps1
```

`setup-local.ps1` will:
1. Create `.venv` and install Python dependencies
2. Verify Ollama is running (`ollama serve` must be active)
3. Pull `qwen3.6:27b-mtp-q4_K_M` (18 GB — only downloads once)
4. Create the `qwen3` alias with 262K context baked in

**The alias is required.** Using the raw model name gives only 32K context, which causes `finish_reason: length` errors during long agentic sessions.

---

## Starting the Stack (Every Session)

### Step 1 — Make sure Ollama is running

Ollama starts automatically with Windows if you used the installer. If not:

```powershell
ollama serve
```

Verify: http://localhost:11434 should respond.

### Step 2 — Start the proxy

```powershell
.\scripts\start-proxy-local.ps1
```

This starts the FastAPI proxy at **http://localhost:8001**. Keep this terminal open — it streams request logs as you use the model.

### Step 3 — Verify

```powershell
Invoke-RestMethod http://localhost:8001/health
# → status: ok, ollama: True
```

Open **http://localhost:8001/dashboard** in your browser for live token throughput.

---

## VS Code Setup

Edit `%APPDATA%\Code\User\chatLanguageModels.json`:

```jsonc
[
  {
    "name": "Local RTX 5090",
    "vendor": "customendpoint",
    "apiKey": "no-key",
    "apiType": "chat-completions",
    "models": [{
      "id": "qwen3",
      "name": "Qwen3.6-27B (RTX 5090)",
      "url": "http://localhost:8001/v1/chat/completions",
      "toolCalling": true,
      "vision": true,
      "maxInputTokens": 120000,
      "maxOutputTokens": 16000,
      "thinking": true,
      "streaming": true
    }]
  }
]
```

**Why `maxOutputTokens: 16000`?** Qwen3 burns 5K-15K tokens on its internal `<think>` monologue before writing output. 16K gives plenty of room without wasting context.

---

## Live Dashboard

While the proxy is running, open **http://localhost:8001/dashboard** in any browser:

- Combined tokens/sec across all active requests
- Per-request token count and TPS
- Total tokens generated this session

---

## Project Structure

```
copilot-local/
├── proxy/
│   ├── main.py              # FastAPI proxy — temp clamping, routing, dashboard
│   ├── tracker.py           # Real-time token throughput tracker (in-memory)
│   ├── requirements.txt     # fastapi, uvicorn, httpx
│   └── Dockerfile           # Optional: containerize the proxy
├── scripts/
│   ├── setup-local.ps1      # ONE-TIME: install deps, pull model, create alias
│   ├── start-proxy-local.ps1 # START THIS every session before using VS Code
│   ├── test-proxy.py        # Smoke test: health check + sample chat request
│   └── warmup.py            # Pre-load model into VRAM after Ollama starts
└── AGENTS.md                # Stack reference for AI coding agents
```

---

## Known Issues

| Error | Cause | Fix |
|---|---|---|
| `ERR_CONNECTION_REFUSED` in VS Code | Proxy not running | Run `.\scripts\start-proxy-local.ps1` |
| `finish_reason: length` / truncated responses | `qwen3` alias missing 262K context | Re-run `.\scripts\setup-local.ps1` |
| `ModuleNotFoundError: fastapi` | Wrong Python / venv not active | Use the proxy via `start-proxy-local.ps1` (calls `.venv\uvicorn.exe` directly) |
| First request is slow | Model not in VRAM yet | Run `python scripts\warmup.py` after starting Ollama |
| `maxOutputTokens` error in VS Code | Cap too low for thinking mode | Set `maxOutputTokens` to 16000 in `chatLanguageModels.json` |
