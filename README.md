<div align="center">

# 🚀 GitHub Copilot Local

### Run Qwen3.6-27B on your GPU · Zero API costs · 262K context · Full privacy · Works in any VS Code project

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Model](https://img.shields.io/badge/Model-Qwen3.6--27B-purple)](https://ollama.com/library/qwen3.6)
[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-green)](https://fastapi.tiangolo.com/)
[![GPU](https://img.shields.io/badge/GPU-RTX--5090-black)](#)
[![Context](https://img.shields.io/badge/Context-262K%20tokens-important)](#)
[![Ollama](https://img.shields.io/badge/Runtime-Ollama-orange)](https://ollama.com/)

> **Private AI coding assistant**, running entirely on your own hardware. Same VS Code experience, zero API costs, complete privacy — no data ever leaves your machine.

</div>

---

## ⚡ Quick Start

```
Clone → Setup (once) → Proxy (every session) → Code
    ~2 min          30 seconds             Instant
```

**One-time setup:**
```powershell
git clone https://github.com/darkmatter2222/GithubCopilotExit.git
cd GithubCopilotExit
.\scripts\setup-local.ps1
```

**Every session after that:**
```powershell
.\scripts\start-proxy-local.ps1
```

Then open VS Code, fire up Copilot Chat, and select **Qwen3.6-27B (RTX 5090)**. You're live. 🎯

---

## ✨ At a Glance

| Feature | Status | Detail |
| ------- | :----: | ------ |
| Full privacy | ✅ | Everything runs locally — no data leaves your machine |
| 262K context | ✅ | Entire codebases fit in a single conversation |
| Tool calling | ✅ | Files, terminals, search — same as cloud Copilot |
| Vision support | ✅ | Image understanding for debugging and diagrams |
| Thinking mode | ✅ | Deep reasoning chains for complex tasks |
| Streaming | ✅ | Real-time token flow, no waiting |
| Live dashboard | ✅ | Command center at `localhost:8001` with TPS charts |
| Zero cost | ✅ | $0/month after GPU investment, forever |

---

## 🏗 How It Works

```
  ┌──────────────────────┐
  │   VS Code Copilot    │  ← You chat here like normal
  │        Chat          │
  └──────────┬───────────┘
             │  POST /v1/chat/completions
             │  (OpenAI-compatible API)
     localhost:8001
             ▼
  ┌──────────────────────┐
  │   FastAPI Proxy      │  ← proxy/main.py
  │                      │    • Clamps temp ≥ 0.6  (thinking mode requirement)
  │                      │    • Rewrites model → "qwen3"
  │                      │    • Tracks tokens       (live dashboard data)
  │                      │    • Streams responses   (SSE streaming)
  └──────────┬───────────┘
             │  /v1/chat/completions
     localhost:11434
             ▼
  ┌──────────────────────┐
  │       Ollama         │  ← Local model server
  │   alias: qwen3       │    (qwen3.6:27b-mtp-q4_K_M, Q4 quant)
  │   context: 262K      │
  └──────────┬───────────┘
             │  PCIe → VRAM (~18 GB)
             ▼
  ╔══════════════════════╗
  ║    RTX 5090 GPU      ║  ← The real horsepower
  ╚══════════════════════╝
```

> **Key insight:** VS Code Copilot Chat already supports custom OpenAI-compatible endpoints. All we need is a thin proxy connecting it to Ollama, with two tricks — clamping temperature for thinking mode and ensuring 262K context via a model alias. The `qwen3` alias is critical; without it, Ollama defaults to 32K context and truncates mid-thought on larger tasks.

---

## 📦 Detailed Setup Guide

### Prerequisites

| Requirement | Version | Notes |
| ----------- | ------- | ----- |
| Windows | 10/11 | PowerShell 5.1+ included |
| Python | 3.12 | Script auto-creates `.venv` for you |
| GPU | RTX 4090 / 5090+ | 24GB+ VRAM recommended (Q4 quant) |
| Disk Space | ~18 GB free | Model download, one-time only |
| VS Code | Latest release | With GitHub Copilot extension installed |

### Step 1 — Clone and Run Setup

```powershell
git clone https://github.com/darkmatter2222/GithubCopilotExit.git
cd GithubCopilotExit
.\scripts\setup-local.ps1
```

This will:
- Create a `.venv` virtual environment and install Python dependencies (FastAPI, Uvicorn, httpx)
- Verify Ollama is installed and reachable at `localhost:11434`
- Pull `qwen3.6:27b-mtp-q4_K_M` (~18 GB, one-time download only)
- Create the **`qwen3` alias with 262K context baked in** (this is critical — see below)

### Step 2 — Configure VS Code (`chatLanguageModels.json`) ⭐

This is where you tell VS Code about your local model. It's a simple JSON file that lives in your user settings folder.

#### Finding the file

The config file lives at:

```
%LOCALAPPDATA%\Programs\Microsoft VS Code\User\chatLanguageModels.json
```

**On Windows, the full expanded path is typically:**
```
C:\Users\<YOUR_USERNAME>\AppData\Roaming\Code\User\chatLanguageModels.json
```

> **Where is this?** `AppData` is a hidden folder. Press `Win + R`, type `%APPDATA%\Code\User\`, and hit Enter — it'll open the folder directly.

#### Creating or editing the file

1. **Navigate** to `%APPDATA%\Code\User\` in File Explorer
2. Look for `chatLanguageModels.json`:
   - **If it exists** → open it with a text editor (VS Code works great)
   - **If it doesn't exist** → create a new file called `chatLanguageModels.json`

#### What to put inside

The file is a JSON array. Each element defines an "endpoint group" of models. If you already have entries here (like OpenAI, Azure, etc.), just **add** the new entry — don't replace existing ones.

```jsonc
[
  // ── YOUR EXISTING ENTRIES CAN STAY HERE ──
  // (e.g., your OpenAI, Anthropic, or Azure configs)

  // ── LOCAL GPU MODEL ──
  {
    "name": "Local RTX 5090",        // Display name (choose whatever you like)
    "vendor": "customendpoint",      // Tells VS Code this is a custom endpoint
    "apiKey": "no-key",             // No auth needed — it's running on your machine
    "apiType": "chat-completions",   // OpenAI-compatible chat API
    "models": [
      {
        "id": "qwen3",              // MUST match the Ollama alias name
        "name": "Qwen3.6-27B (RTX 5090)",  // What you'll see in the Copilot picker
        "url": "http://localhost:8001/v1/chat/completions",
        "toolCalling": true,         // Enable file edits, terminal commands, etc.
        "vision": true,             // Enable image/screenshot understanding
        "maxInputTokens": 120000,    // Generous window (hard cap is 262K)
        "maxOutputTokens": 16000,   // Enough for detailed code responses
        "thinking": true,            // Enable deep reasoning chains
        "streaming": true            // Real-time token streaming (responsive feel)
      }
    ]
  }
]
```

#### Field-by-field breakdown

| Field | Value | Why it matters |
| ----- | ----- | -------------- |
| `name` | `"Local RTX 5090"` | Group label shown in VS Code — customize to your GPU |
| `vendor` | `"customendpoint"` | Tells VS Code this isn't a built-in provider |
| `apiKey` | `"no-key"` | Required field but ignored — everything is local |
| `apiType` | `"chat-completions"` | Uses OpenAI's chat completions format |
| **→ models[].id** | `"qwen3"` | **MUST match the Ollama alias exactly** — case sensitive |
| **→ models[].name** | `"Qwen3.6-27B (RTX 5090)"` | What appears in the Copilot Chat model dropdown |
| **→ models[].url** | `http://localhost:8001/v1/chat/completions` | Points to your proxy — must have trailing path |
| `toolCalling` | `true` | Enables file/terminal/search tool operations |
| `vision` | `true` | Enables screenshot/image understanding |
| `maxInputTokens` | `120000` | How much context you can send (model supports up to 262K) |
| `maxOutputTokens` | `16000` | Max response length — too low cuts off thinking mode |
| `thinking` | `true` | Enables the model's internal reasoning chain |
| `streaming` | `true` | Tokens appear progressively as they're generated |

#### Common mistakes to avoid

| Mistake | Symptom | Fix |
| ------- | ------- | --- |
| Wrong `id` value | "Model not found" errors | Must be exactly `"qwen3"` — matching your Ollama alias |
| Wrong URL | Connection refused | Must include full path: `/v1/chat/completions` at the end |
| Missing trailing comma issues | JSON parse error | If adding to existing array, ensure commas between entries |
| `maxOutputTokens` too low | Thinking mode cuts off | Set to at least `16000` |
| File saved in wrong location | VS Code ignores it | Must be in `%APPDATA%\Code\User\` — not project folder |

#### Verify the config took effect

```powershell
# In PowerShell, quickly check the file exists and has content
Test-Path "$env:APPDATA\Code\User\chatLanguageModels.json"
# → True ✅

# View it to confirm correctness
cat "$env:APPDATA\Code\User\chatLanguageModels.json"
# Should show your "Local RTX 5090" entry with model id "qwen3"
```

> **After saving**, reload VS Code (`Ctrl+Shift+P` → "Reload Window"). The new model will appear in the Copilot Chat picker at the bottom of the chat panel.

### Step 3 — Start Coding 🚀

Every session:

**1. Ensure Ollama is running** (auto-starts with Windows on most setups):
```powershell
# Quick check — should return model list
Invoke-RestMethod http://localhost:11434/api/tags
# If not, start it: ollama serve
```

**2. Start the proxy:**
```powershell
.\scripts\start-proxy-local.ps1
```

Keep this terminal open. It streams every request as you code.

**3. (Optional but recommended) Warm up VRAM:**
```powershell
python scripts\warmup.py
```
This pre-loads the model into GPU memory so your first real request doesn't have a 20-30 second cold start.

**4. Open VS Code**, open Copilot Chat (`Ctrl+Shift+I`), select **Qwen3.6-27B (RTX 5090)** from the model picker, and start coding.

---

## ✅ Verifying Everything Works

```powershell
# Health check — proxy + Ollama connectivity
Invoke-RestMethod http://localhost:8001/health
# → @{status=ok; ollama=True}   ✅

# Full smoke test (health + real completion)
python scripts\test-proxy.py
# → all tests pass   ✅
```

Open **http://localhost:8001/dashboard** in a browser for the full command center view. Auto-refreshes every 2 seconds:

| Panel | What You See |
| ----- | ------------ |
| **Session Stats** | Uptime, total requests, success/error counts |
| **Throughput Meter** | Live tokens-per-second and active request count |
| **TPS Sparkline** | Rolling throughput graph (~2 min window) |
| **Input vs Output** | Per-request token breakdown bars |
| **Active Requests** | Real-time token counts and elapsed time |
| **Request History** | Completed requests with timing |
| **Event Log** | Timestamped INFO/ERROR feed |

---

## 📂 Project Structure

```
GithubCopilotExit/
├── proxy/
│   ├── main.py              # FastAPI proxy — temp clamping, model rewrite, streaming
│   ├── tracker.py           # Thread-safe token throughput tracker (in-memory)
│   ├── dashboard.html       # Live command-center dashboard with real-time charts
│   ├── requirements.txt     # fastapi, uvicorn, httpx
│   └── Dockerfile           # Container support for Unix environments
├── scripts/
│   ├── setup-local.ps1      # ONE-TIME: create .venv, pull model, set up alias
│   ├── start-proxy-local.ps1 # EVERY SESSION: starts the proxy server
│   ├── test-proxy.py        # Smoke tests: health check + sample inference
│   └── warmup.py            # Pre-loads model into VRAM for faster cold starts
├── AGENTS.md                # Stack reference doc for AI coding agents
└── README.md                # ← You are here
```

---

## 🔧 Tuning & Configuration

| Setting | How to Change | Default | Notes |
| ------- | ------------- | ------- | ----- |
| Ollama base URL | `OLLAMA_BASE_URL` env var | `http://localhost:11434` | If Ollama runs on a different host/port |
| Served model name | `SERVED_MODEL_NAME` env var | `qwen3` | Must match your Ollama alias Name |
| Min temperature | `MIN_TEMPERATURE` env var | `0.6` | Qwen3 thinking mode requires ≥ 0.6 |
| Max input tokens | VS Code config (`maxInputTokens`) | `120000` | Hard cap is 262K (model limit) |
| Max output tokens | VS Code config (`maxOutputTokens`) | `16000` | Higher = longer responses, more VRAM pressure |

### Running on Different GPUs

While the reference setup targets an RTX 5090, this stack works with various GPU configurations:

| GPU | VRAM | Recommended Model | Performance |
| --- | ---- | ----------------- | ----------- |
| RTX 5090 | 32 GB | qwen3.6:27b Q4 (default) | Excellent — fast generation |
| RTX 4090 | 24 GB | qwen3.6:27b Q4 | Great — works well |
| RTX 3090 | 24 GB | qwen3.6:27b Q4 | Good — solid performance |
| < 24 GB VRAM | -- | Consider smaller quant or model | Varies by setup |

To swap models, update the pull command and alias in `scripts/setup-local.ps1`, then adjust `"id"` in your `chatLanguageModels.json`.

---

## 🏆 Architecture Decisions

| Design Choice | Rationale |
| ------------- | --------- |
| **Thin proxy** over SDK integration | OpenAI-compatible endpoint means zero VS Code changes needed |
| **Temperature clamping** (≥ 0.6) | Qwen3 thinking mode completely breaks at VS Code's default of 0.1 |
| **262K context via alias** | Ollama defaults to 32K without it — far too small for real coding sessions |
| **No timeouts** (`timeout=None`) | Complex reasoning can take minutes; let the model finish its thought |
| **In-memory tracker** | Zero I/O overhead, no database — stats are per-session |
| **No credentials committed** | No hardcoded IPs, keys, or `.env` files in the repo |

---

## 🌟 Why Go Local?

- **🔒 Privacy first** — Your code never leaves your machine. No telemetry, no cloud processing, no data pipelines you don't control
- **💰 Zero recurring costs** — After the GPU purchase, it's free forever. Enterprise Copilot is $19/user/month; this is $0
- **⚡ No rate limits** — Generate unlimited tokens. Run parallel coding sessions if your VRAM can handle it
- **📖 Full context control** — 262K tokens means you can paste entire codebases, large diffs, and complex multi-file refactors without overflow
- **🌍 Works offline** — No internet required after setup. Great for secure environments, airplanes, and disconnected development

---

## 🐛 Troubleshooting

| Symptom | Root Cause | Fix |
| ------- | ---------- | --- |
| `ERR_CONNECTION_REFUSED` in VS Code | Proxy not running | Run `\scripts\start-proxy-local.ps1` |
| Response cuts off mid-reply | `qwen3` alias missing 262K context | Re-run `.\scripts\setup-local.ps1` |
| `ModuleNotFoundError: fastapi` | System Python instead of `.venv` | Always use `start-proxy-local.ps1` — it calls `.venv\uvicorn.exe` directly |
| First request takes 20-30 extra seconds | Model not yet in VRAM (cold start) | Run `python scripts\warmup.py` after starting Ollama |
| `maxOutputTokens` error in VS Code | Output cap too low for thinking mode | Set `"maxOutputTokens": 16000` in config |
| Ollama unreachable at `localhost:11434` | Ollama not running | Start Ollama, or run `ollama serve` manually |

---

## 🤝 Contributing

Contributions welcome! Bug fixes, new features, improved docs, GPU compatibility guides — every contribution is appreciated.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Submit a Pull Request

---

## ⭐ Support This Project

If this saved you from cloud API costs or gave you peace of mind about privacy, consider leaving a **⭐ star** — it helps others discover the project.

<div align="center">

### Happy coding locally! 🚀

[MIT License](LICENSE) — Free forever, no strings attached.

</div>
