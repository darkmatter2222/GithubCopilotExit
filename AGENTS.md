# Agent Operating Instructions

## Stack Overview

**PRIMARY: NVIDIA DGX Spark (GB10 Superchip)** — deployed at `192.168.86.39`
**BACKUP: Local RTX 5090** — Windows machine, still running for failover (do not tear down)

### DGX Spark Stack (Primary)
```
  VS Code Copilot Chat
        │  http://192.168.86.39:8001
        ▼
  FastAPI Proxy  (Docker container: gcopilot-proxy)
        │  http://localhost:11434  (inside container, host network)
        ▼
  Ollama  (systemd service, Ubuntu 24.04 aarch64)
        │  CUDA0
        ▼
  NVIDIA GB10  (122 GB unified memory, Blackwell native FP4 support)
```

### Local RTX 5090 Stack (Backup — DO NOT TEAR DOWN)
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

## DGX Spark Details

| Setting | Value |
|---|---|
| Hostname | `dgxspark` (add to SSH config) |
| IP | `192.168.86.39` |
| Username | `darkmatter2222` |
| GPU | NVIDIA GB10 Grace Blackwell superchip |
| GPU Memory | 122 GB unified LPDDR5x |
| OS | Ubuntu 24.04 LTS (aarch64/ARM) |
| CPU | ARM aarch64, 20 threads |
| Ollama | v0.30.10 at `/usr/local/bin/ollama` (ARM native binary) |
| Model | `qwen3.6:27b-mtp-q4_K_M` (~17 GB Q4 quantized) |
| Alias | `qwen3` with num_ctx=131000 via Modelfile |
| Proxy | Docker container `gcopilot-proxy` (port 8001, host network) |
| MongoDB | Connected to `192.168.86.48:27017` (persistent analytics) |

### DGX Spark Performance Benchmarks
```
Prompt processing : 219 TPS  (22 tokens / 100ms)
Token generation  : 40 TPS   (20 tokens / 502ms)
GPU               : CUDA0 all 66/66 layers offloaded
Blackwell FP4     : BLACKWELL_NATIVE_FP4 = 1 (native support enabled)
```

### SSH Config
```# ~/.ssh/config
Host dgxspark
    HostName 192.168.86.39
    User darkmatter2222
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
```

---

## Starting the Stack (DGX Spark — Primary)

The DGX Spark runs as a systemd service + Docker container. No manual start needed — it's always on.

**Verify everything is running:**
```powershell
# Health check
Invoke-RestMethod http://192.168.86.39:8001/health
# Expected: status=ok, ollama=True

# Dashboard (MongoDB connected)
Start-Process http://192.168.86.39:8001/dashboard
```

### Starting the Stack (Local RTX 5090 — Backup)

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
| Connection | `192.168.86.48:27017` (persistent Docker MongoDB, both DGX Spark and local) |
| Database | `radiacode` |
| Collection | `requests` |
| Auth | `ryan` / configured in `.env` |

**Fields stored per request:** `request_id`, `model`, `prompt_tokens`, `completion_tokens`

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

### DGX Spark (Primary)
```
Deploy everything     : python scripts/final-deploy.py           ← needs $env:DGXSPARK_SUDO_PASS
Fix MongoDB only      : python scripts/fix_mongo_only.py         ← interactive password prompt
SSH to device         : ssh dgxspark
Dashboard             : http://192.168.86.39:8001/dashboard
Health check          : curl http://192.168.86.39:8001/health
Benchmark             : ssh dgxspark 'python3 ~/cbench.py'
```

### Local RTX 5090 (Backup)
```powershell
First-time setup     : .\scripts\setup-local.ps1
Every session (all)  : .\scripts\go.ps1          ← one command does everything
Start proxy only     : .\scripts\start-proxy-local.ps1
Smoke test           : python scripts\test-proxy.py
Warm up VRAM         : python scripts\warmup.py
Build                : NOT APPLICABLE
Unit tests           : NOT APPLICABLE (smoke tests only)
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
| `proxy/tracker.py` | Thread-safe real-time token throughput tracker (in-memory, no DB) |
| `proxy/db.py` | MongoDB persistence layer (async via motor) |
| `proxy/cost_engine.py` | Cost calculation engine for analytics |
| `proxy/dashboard.html` | Full command-center dashboard — live charts, session stats, request history |
| `proxy/Dockerfile` | Docker image definition for DGX Spark deployment |
| `scripts/setup-local.ps1` | Local RTX 5090 one-time setup: .venv creation, model pull, qwen3 alias |
| `scripts/start-proxy-local.ps1` | Start local proxy — run every session |
| `scripts/final-deploy.py` | Deploy everything to DGX Spark (SFTP upload + Docker build/run) |
| `scripts/fix_mongo_only.py` | Reload DGX Spark container with MongoDB env vars |

---

## Security

- **No credentials in repo** — `.env` is gitignored, no hardcoded IPs or keys committed
- **Deployment scripts** (`final-deploy.py`, `dgxspark_reload.py`) are gitignored
- **Sudo password** must be set via env var (`$env:DGXSPARK_SUDO_PASS`) at runtime — never stored

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
