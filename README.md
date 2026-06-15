# Copilot-Local — Private AI Coding Assistant on Your Own GPU

Run **Qwen3.6 27B** on your RTX 5090. Zero API costs. Full 262K context. Thinking mode. Tool calling. Vision. Works in any VS Code project.

---

## Every Time You Boot Up — Start Here

You need two things running before VS Code can use the model:

1. **Ollama** (serves the model) — auto-starts with Windows. If it's not running:
   ```powershell
   ollama serve
   ```

2. **The proxy** (connects VS Code to Ollama) — run this every session:
   ```powershell
   cd C:\path\to\GithubCopilotExit
   .\scripts\start-proxy-local.ps1
   ```
   Keep this terminal open. It logs every request as you code.

That's it. Open VS Code, open Copilot Chat, select **"Qwen3.6-27B (RTX 5090)"** from the model picker, and start coding.

---

## Verify It's Working

```powershell
Invoke-RestMethod http://localhost:8001/health
# → status: ok, ollama: True   ✅
```

Open **http://localhost:8001/dashboard** in a browser for a full command-center view while the model is generating. The dashboard auto-refreshes every 2 seconds and shows:

- **Session stats** — uptime, total requests, success/error counts
- **Throughput meter** — live TPS, active requests, total input & output tokens
- **TPS sparkline chart** — rolling token-per-second graph (last ~2 min)
- **Input vs Output bars** — per-request token breakdown for recent sessions
- **Active request table** — real-time token counts and elapsed time
- **Request history** — full log of completed requests with timing
- **Event log** — timestamped INFO/ERROR feed for debugging

---

## First-Time Setup (Run Once After Cloning)

```powershell
git clone https://github.com/darkmatter2222/GithubCopilotExit.git
cd GithubCopilotExit
.\scripts\setup-local.ps1
```

`setup-local.ps1` will:
1. Create `.venv` and install Python dependencies
2. Verify Ollama is installed and running
3. Pull `qwen3.6:27b-mtp-q4_K_M` — **~18 GB download, one time only**
4. Create the `qwen3` alias with 262K context baked in

**The alias matters.** Without it, the model runs with only 32K context window, causing responses to get cut off mid-thought on long agentic tasks.

---

## VS Code Configuration

Edit `%APPDATA%\Code\User\chatLanguageModels.json` — do this once:

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

After saving, reload VS Code. The model will appear in the Copilot Chat model picker.

---

## How It Works

```
  VS Code Copilot Chat
        │
        │  http://localhost:8001  (OpenAI-compatible API)
        ▼
  FastAPI Proxy  ──  proxy/main.py
        │  Clamps temperature to >= 0.6 (Qwen3 thinking mode requires this)
        │  Rewrites model name to "qwen3"
        │  Tracks token throughput for dashboard
        │
        │  http://localhost:11434
        ▼
  Ollama  (local install)
        │  model alias: qwen3  (262K context)
        │
        │  PCIe
        ▼
  RTX 5090  ─  ~18 GB VRAM in use
  qwen3.6:27b-mtp-q4_K_M (Q4_K_M quantization)
```

Everything is local. No cloud. No API key. No data leaves your machine.

---

## Project Files

```
GithubCopilotExit/
├── proxy/
│   ├── main.py              # FastAPI proxy — the core of this stack
│   ├── tracker.py           # Real-time token throughput tracker (in-memory)
│   └── requirements.txt     # fastapi, uvicorn, httpx
├── scripts/
│   ├── setup-local.ps1      # ONE-TIME: install deps, pull model, create alias
│   ├── start-proxy-local.ps1 # RUN EVERY SESSION: starts the proxy
│   ├── test-proxy.py        # Smoke test: health check + sample inference
│   └── warmup.py            # Pre-loads model into VRAM after cold start
└── AGENTS.md                # This stack's reference doc for AI coding agents
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ERR_CONNECTION_REFUSED` in VS Code | Proxy not running | Run `.\scripts\start-proxy-local.ps1` |
| Response cuts off mid-reply | `qwen3` alias missing 262K context | Re-run `.\scripts\setup-local.ps1` |
| `ModuleNotFoundError: fastapi` when starting proxy | Wrong Python / venv issue | Always use `start-proxy-local.ps1` — it calls `.venv\uvicorn.exe` directly |
| First request takes 20-30 extra seconds | Model not yet in VRAM | Run `python scripts\warmup.py` once after Ollama starts |
| `maxOutputTokens` error in VS Code | Output cap too low for thinking mode | Set `"maxOutputTokens": 16000` in `chatLanguageModels.json` |
