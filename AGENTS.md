# Agent Operating Instructions

## Stack Overview

This repo runs a local AI coding assistant on a Windows machine with an RTX 5090. Everything runs locally — no Docker, no cloud, no remote servers.

```
  VS Code Copilot Chat
        │  http://localhost:8001
        ▼
  FastAPI Proxy  (proxy/main.py)
        │  http://localhost:11434
        ▼
  Ollama  (Windows install — ollama.com)
        │  PCIe
        ▼
  RTX 5090  (~18 GB VRAM for Q4 weights, ~128K tokens usable from KV cache)
```

---

## Starting the Stack (Every Session)

**Step 1 — Ollama must be running**
```powershell
# Ollama auto-starts with Windows. If not:
ollama serve
# Verify: http://localhost:11434 should respond
```

**Step 2 — Start the proxy**
```powershell
.\scripts\start-proxy-local.ps1
```
Keep this terminal open. The proxy logs every request.

**Step 3 — Verify**
```powershell
Invoke-RestMethod http://localhost:8001/health
# Expected: status=ok, ollama=True
```

Live dashboard: **http://localhost:8001/dashboard** — comprehensive command center with live TPS sparkline, input/output bar charts, session stats (uptime, requests, errors), active request table, request history, and timestamped event log. Auto-refreshes every 2s.

---

## First-Time Setup (Once Per Machine)

```powershell
.\scripts\setup-local.ps1
```

Installs Python deps into `.venv`, pulls the model (~18 GB), and creates the `qwen3` alias with large context window (`num_ctx=262144`). Run once after cloning.

**The `qwen3` alias is critical.** Without it, Ollama defaults to 32K context, causing `finish_reason: length` errors on long agentic sessions. Actual usable context depends on GPU VRAM — ~128K on RTX 5090 (32GB), ~32K on 24GB GPUs due to KV cache limits.

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
| `GET  /dashboard` | Full command-center dashboard — TPS sparkline, input/output charts, session stats, event log, request history (auto-refreshes 2s) |
| `GET  /stats` | Raw JSON stats (active requests, combined TPS, total tokens) |

---

## Repository Commands

```
First-time setup    : .\scripts\setup-local.ps1
Start proxy         : .\scripts\start-proxy-local.ps1
Smoke test          : python scripts\test-proxy.py
Warm up VRAM        : python scripts\warmup.py
Build               : NOT APPLICABLE
Unit tests          : NOT APPLICABLE (smoke tests only)
```

---

## Known Issues

| Error | Root Cause | Fix |
|---|---|---|
| `ERR_CONNECTION_REFUSED` | Proxy not running | Run `start-proxy-local.ps1` |
| `finish_reason: length` / truncated | VRAM exhausted (KV cache full) or alias misconfigured | Re-run `setup-local.ps1`; lower `maxInputTokens` for 24GB GPUs |
| `ModuleNotFoundError: fastapi` | System Python instead of .venv | Use `start-proxy-local.ps1` — calls `.venv\uvicorn.exe` directly |
| First request slow (20-30s extra) | Model not in VRAM | Run `python scripts\warmup.py` after starting Ollama |
| `maxOutputTokens` error | Cap too low for thinking mode | Set `maxOutputTokens: 16000` in `chatLanguageModels.json` |

---

## Architecture and Conventions

- **Language/framework:** Python 3.12, FastAPI, httpx, uvicorn
- **All timeouts:** `None` (no hard limits — local model can take as long as it needs)
- **Testing:** Smoke tests via `scripts/test-proxy.py`
- **Error handling:** Proxy passes Ollama errors through unchanged with original HTTP status codes
- **Logging:** stdout, INFO level, timestamped
- **No credentials in repo:** No hardcoded IPs, keys, or `.env` files committed

### Key Files

| File | Purpose |
|---|---|
| `proxy/main.py` | FastAPI proxy — temp clamping, model name rewrite, streaming, token tracking, error handling |
| `proxy/tracker.py` | Thread-safe real-time token throughput tracker (in-memory, no DB) — input/output tokens, timing, event log, chart data |
| `proxy/dashboard.html` | Full command-center dashboard — live charts, session stats, request history, event log |
| `scripts/setup-local.ps1` | One-time setup: .venv creation, model pull, qwen3 alias |
| `scripts/start-proxy-local.ps1` | Start the proxy — run every session |
| `scripts/test-proxy.py` | Smoke test: /health + /v1/chat/completions |
| `scripts/warmup.py` | Pre-loads model into VRAM so first request isn't slow |

---

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
