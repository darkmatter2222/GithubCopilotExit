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

Live dashboard: **http://localhost:8001/dashboard** — redesigned Analytics UI with:
- **Live tab**: TPS sparkline, I/O bar chart, active request table, session history (Chart.js, auto-refresh 2s)
- **History tab**: Full MongoDB request log with 24h/7d/30d/90d time range filters
- **Usage tab**: Daily & hourly token usage charts (input vs output, request count)
- **Event Log tab**: Filterable proxy event stream (ALL / INFO / WARN / ERROR)

---

## ⚠️ Critical: Modifying the Proxy (Self-Deploy Protocol)

**The VS Code Copilot chat session runs THROUGH this proxy.** Restarting the proxy will terminate the current AI session mid-response.

**Rules to follow every time you modify and redeploy:**
1. Make ALL code changes to every file BEFORE issuing any restart commands.
2. Install any new Python dependencies BEFORE restarting.
3. Issue the kill + restart as a **single chained PowerShell command** so the proxy comes back up automatically (no second turn needed):
   ```powershell
   Stop-Process -Id (Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue).OwningProcess -Force -ErrorAction SilentlyContinue ; Start-Sleep -Milliseconds 500 ; .\scripts\start-proxy-local.ps1
   ```
4. **Expect your session to be terminated.** This is normal. The new proxy process will start and VS Code will reconnect automatically on the next message.
5. Never restart the proxy as a mid-task step — only as the final action after all changes are complete and validated.

---

## MongoDB Persistence

The proxy now stores every request to MongoDB for historical analytics.

| Setting | Value |
|---|---|
| Connection | `host.docker.internal:27017` (Docker MongoDB) |
| Database | `radiacode` |
| Collection | `requests` |
| Auth | `ryan` / configured in `.env` |

**Fields stored per request:** `request_id`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `duration_secs`, `ttft_secs`, `tps`, `has_error`, `timestamp`

Dashboard API endpoints (all MongoDB-backed):
- `GET /api/history?days=30&limit=200` — raw request log
- `GET /api/usage/daily?days=30` — aggregated daily token usage
- `GET /api/usage/hourly?days=7` — aggregated hourly token usage
- `GET /api/stats/summary?days=30` — summary KPIs

If MongoDB is unavailable, the proxy runs in memory-only mode (live data still works; History/Usage dashboard pages show "not connected").

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
Every session (all) : .\scripts\go.ps1          ← one command does everything
Start proxy only    : .\scripts\start-proxy-local.ps1
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
