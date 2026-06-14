# Agent Operating Instructions

## Stack Overview

This repo runs a local AI coding assistant stack on a Windows machine with an RTX 5090. Everything is local — no remote servers, no Docker, no cloud.

```
  VS Code Copilot Chat
        │  localhost:8001
        ▼
  FastAPI Proxy  (proxy/main.py)    ← start with: .\scripts\start-proxy-local.ps1
        │  localhost:11434
        ▼
  Ollama  (local install)           ← must be running: ollama serve
        │  PCIe
        ▼
  RTX 5090  (~18 GB VRAM in use)
  Model: qwen3  (qwen3.6:27b-mtp-q4_K_M, Q4_K_M, 262K context)
```

---

## How to Start the Stack

**Step 1 — Ensure Ollama is running**
```powershell
# Ollama should auto-start with Windows. If not:
ollama serve
# Verify: http://localhost:11434 should respond
```

**Step 2 — Start the proxy**
```powershell
.\scripts\start-proxy-local.ps1
```

**Step 3 — Verify**
```powershell
Invoke-RestMethod http://localhost:8001/health
# Expected: status=ok, ollama=True
```

Dashboard: **http://localhost:8001/dashboard** — live token throughput, auto-refreshes every 2s.

---

## First-Time Setup (Once Per Machine)

```powershell
.\scripts\setup-local.ps1
```

This installs Python deps, pulls the model (~18 GB), and creates the `qwen3` alias with 262K context. Must be done once after cloning.

**The `qwen3` alias is critical.** Without it the model defaults to 32K context, causing `finish_reason: length` on long sessions.

---

## VS Code Client Config (`%APPDATA%\Code\User\chatLanguageModels.json`)

```json
[{
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
}]
```

---

## Proxy Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat (VS Code talks here) |
| `GET  /health` | Health check — returns `{"status":"ok","ollama":true}` |
| `GET  /dashboard` | Live token throughput dashboard (HTML, 2s refresh) |

---

## Known Issues Reference

| Error | Root Cause | Fix |
|---|---|---|
| `ERR_CONNECTION_REFUSED` in VS Code | Proxy not running | Run `start-proxy-local.ps1` |
| `finish_reason: length` / truncated | `qwen3` alias has 32K not 262K context | Re-run `setup-local.ps1` |
| `ModuleNotFoundError: fastapi` | System Python used instead of .venv | `start-proxy-local.ps1` calls `.venv\uvicorn.exe` directly — use the script |
| First request slow | Model not in VRAM | Run `python scripts\warmup.py` after Ollama starts |
| `maxOutputTokens` error | Cap too low for thinking mode (5K-15K tokens burned on `<think>`) | Set `maxOutputTokens: 16000` in `chatLanguageModels.json` |

---

## Repository Commands

```
First-time setup    : .\scripts\setup-local.ps1
Start proxy         : .\scripts\start-proxy-local.ps1
Smoke test          : python scripts\test-proxy.py
Warm up VRAM        : python scripts\warmup.py
Build               : NOT APPLICABLE (no compiled artifacts)
Unit tests          : NOT APPLICABLE (integration-only smoke tests)
```

## Architecture and Conventions

- **Language/framework:** Python 3.12, FastAPI, httpx, uvicorn (proxy)
- **Testing:** Smoke tests only via `scripts/test-proxy.py`
- **Error handling:** Proxy passes Ollama errors through unchanged with original HTTP status codes
- **Logging:** `logging.basicConfig` to stdout, INFO level, timestamped
- **Dependencies:** `pip` with pinned versions in `proxy/requirements.txt`
- **No credentials in repo:** No hardcoded IPs, usernames, or keys. No `.env` file committed.

### Key Files

| File | Purpose |
|---|---|
| `proxy/main.py` | FastAPI proxy — temp clamping, model name rewrite, streaming, dashboard |
| `proxy/tracker.py` | Thread-safe real-time token throughput tracker (in-memory, no DB) |
| `scripts/setup-local.ps1` | One-time setup: venv, model pull, qwen3 alias creation |
| `scripts/start-proxy-local.ps1` | Start the proxy every session — keeps terminal open |
| `scripts/test-proxy.py` | Smoke test: /health + /v1/chat/completions |
| `scripts/warmup.py` | Pre-loads model into VRAM so first request isn't slow |

## Completion Report Format

Before reporting completion, provide:

1. **What changed** — which files were modified and why
2. **Validation commands executed** — exact commands, in order
3. **Validation results** — pass/fail for each, with relevant output
4. **Assumptions made** — anything inferred rather than explicitly stated
5. **Remaining limitations** — if any exist after completion

## Prohibited Shortcuts

- Do not claim completion without running validation commands
- Do not suppress errors, remove assertions, or disable checks to get a passing result
- Do not introduce hardcoded credentials, secrets, or insecure defaults
- Do not invent API signatures or module paths — read the source first
