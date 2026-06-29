# Agent Operating Instructions

## Stack Overview

**PRIMARY: NVIDIA DGX Spark (GB10 Superchip)** — `192.168.86.39`
**BACKUP: Local RTX 5090** — Windows machine (do not tear down)

### Architecture

```
VS Code Copilot Chat (chatLanguageModels.json)
        |  http://192.168.86.39:8001
        v
FastAPI Proxy  (Docker container: gcopilot-proxy, --network host)
        |  http://localhost:11434  (Ollama, host network)
        v
Ollama  (systemd service, Ubuntu 24.04 aarch64)
        |  CUDA0 — 66/66 layers offloaded
        v
NVIDIA GB10  (122 GB unified LPDDR5x memory)
```

The proxy is **fully dynamic** — it discovers models from Ollama automatically every 30 seconds.
No code changes are ever needed to add, remove, or swap models.

---

## DGX Spark Details

| Setting | Value |
|---|---|
| IP / Hostname | `192.168.86.39` / `dgxspark` |
| Username | `darkmatter2222` |
| GPU | NVIDIA GB10 Grace Blackwell — 122 GB unified memory |
| OS | Ubuntu 24.04 LTS (aarch64) |
| Ollama | systemd service at `/usr/local/bin/ollama` |
| Proxy | Docker container `gcopilot-proxy` (port 8001, host network) |
| MongoDB | `192.168.86.48:27017` (persistent analytics) |

### SSH Config
```
Host dgxspark
    HostName 192.168.86.39
    User darkmatter2222
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
```

### Performance Benchmarks
```
Prompt processing : ~219 TPS
Token generation  : ~40 TPS
GPU               : CUDA0, all 66/66 layers offloaded
Blackwell FP4     : native support enabled (BLACKWELL_NATIVE_FP4=1)
```

---

## Key URLs

| URL | Purpose |
|---|---|
| `http://192.168.86.39:8001/health` | Health check — JSON |
| `http://192.168.86.39:8001/dashboard` | Live analytics dashboard |
| `http://192.168.86.39:8001/v1/models` | All available models (OpenAI format) |
| `http://192.168.86.39:8001/stats` | Real-time token stats (JSON) |
| `http://192.168.86.39:8001/api/models/running` | Models currently in VRAM |
| `POST http://192.168.86.39:8001/api/router/refresh` | Force model re-discovery |

---

## Managing Models (Dynamic — No Code Changes Needed)

### List all downloaded models
```bash
ssh dgxspark ollama list
```

### Download a new model (auto-available to proxy within 30s)
```bash
ssh dgxspark "ollama pull qwen3:30b"
ssh dgxspark "ollama pull llama3.3:70b-instruct-q4_K_M"
ssh dgxspark "ollama pull deepseek-r1:32b"
```
Once downloaded, the proxy discovers it at the next refresh (<=30s). No restart needed.
Ollama loads it into VRAM automatically on the first completion request.

### Remove a model
```bash
ssh dgxspark "ollama rm <model-name>"
```

### Check what is loaded in VRAM right now
```bash
ssh dgxspark ollama ps
# Or via proxy API:
curl http://192.168.86.39:8001/api/models/running
```

### Currently available models on DGX Spark
```
qwen3:latest                       17 GB  (default general-purpose)
qwen3.6:27b-mtp-q4_K_M            17 GB  (MTP variant)
qwen3-coder:latest                 18 GB  (coding specialist)
obliterated:latest                 16 GB  (uncensored finetune)
```

### Switch the active model in VS Code Copilot
1. Edit `chatLanguageModels.json` (`%APPDATA%\Code\User\chatLanguageModels.json`)
2. Change the `"id"` field to any model name Ollama has downloaded
3. The proxy routes to it automatically — Ollama loads into VRAM on first request

### Force proxy to re-discover models immediately
```bash
curl -X POST http://192.168.86.39:8001/api/router/refresh
```

### Create a custom Ollama alias (larger context window, etc.)
```bash
ssh dgxspark
cat > ~/Modelfile-custom << 'EOF'
FROM qwen3:latest
PARAMETER num_ctx 131072
PARAMETER temperature 0.6
EOF
ollama create mymodel -f ~/Modelfile-custom
# Proxy discovers "mymodel" within 30 seconds automatically
```

---

## Deploying the Proxy

### Full deploy (upload code + rebuild Docker image on DGX)
```powershell
# From repo root on Windows
python scripts/deploy.py
```

### Quick restart (no code changes)
```powershell
ssh dgxspark "sudo docker restart gcopilot-proxy"
```

### View container logs
```powershell
ssh dgxspark "sudo docker logs gcopilot-proxy --tail 50 -f"
```

### Manual Docker run (reference)
```bash
sudo docker run -d --name gcopilot-proxy --network host \
  --restart unless-stopped \
  -e OLLAMA_BASE_URL=http://localhost:11434 \
  -e MIN_TEMPERATURE=0.6 \
  -e DISABLE_THINKING_FOR_TOOLS=true \
  -e ROUTER_REFRESH_S=30 \
  -e MONGO_URI="mongodb://ryan:PASS@192.168.86.48:27017/radiacode?authSource=radiacode" \
  -e MONGO_DB=radiacode \
  gcopilot-proxy
```

---

## Local RTX 5090 (Backup)

```powershell
# First-time setup (run once per machine)
.\scripts\setup-local.ps1

# Start proxy for the session
.\scripts\start-proxy-local.ps1

# Verify
Invoke-RestMethod http://localhost:8001/health
```

Dashboard: `http://localhost:8001/dashboard`

---

## Databricks Dashboard Deployment

The dashboard runs on Databricks (192.168.86.48) behind nginx ingress at `/copilot/` path prefix.

### Architecture
```
Browser → nginx /copilot/ → serve.py:3002 → DGX Spark :8001
           (strips prefix)    (normalizes paths)  (proxies to Ollama)
```

### Deploy Checklist (DO NOT SKIP STEPS)
1. Edit `dashboard/index.html` and/or `dashboard/serve.py` locally
2. SCP files to Databricks: `scp dashboard/* databricks:~/GithubCopilotExit/dashboard/`
3. Build image with **--no-cache**: `docker build --no-cache -f Dockerfile.deploy -t gcopilot-dashboard .`
4. Stop + remove old container: `docker stop gcopilot-dashboard && docker rm gcopilot-dashboard`
5. Run new container with EXACT env vars:
   ```bash
   docker run -d --name gcopilot-dashboard \
     --restart unless-stopped \
     --network docucraft_docucraft-network \
     -p 3002:3002 \
     -e PROXY_BACKEND=http://192.168.86.39:8001 \
     -e DASHBOARD_PORT=3002 \
     -e PROXY_PATH_PREFIX=/copilot \
     gcopilot-dashboard
   ```
6. Validate via nginx: `curl -sk https://127.0.0.1/copilot/stats -H "Host: susmannet.duckdns.org"`

### Critical Gotchas (read TROUBLESHOOTING.md)
- **Use `PROXY_BACKEND`** not `PROXY_URL` — wrong var causes 502 errors
- **Use `docucraft_docucraft-network`** not `--network host` — nginx Docker DNS resolves container name
- **Always `--no-cache` on build** — cached layers ignore code changes
- **JS fetch calls must use `__bp + '/path'`** — hardcoded paths break behind nginx `/copilot/ prefix
- **serve.py must normalize paths** — `_norm_path = self.path.split("?")[0]` before routing

### Extensions
Use `validate-databricks-dashboard` and `sync-dashboard-databricks` tools from deploy-dgx extension.

---

## Repository Structure

```
proxy/
  main.py              Dynamic FastAPI proxy (single /v1/chat/completions)
  router.py            Backend router — auto-discovers Ollama + vLLM models
  tracker.py           Thread-safe token throughput tracker (in-memory)
  db.py                MongoDB async persistence layer
  cost_engine.py       Cloud cost comparison engine
  requirements.txt     Python dependencies
  Dockerfile           Docker image definition
  dashboard.html       Served at /dashboard (copy of dashboard/index.html)
dashboard/
  index.html           Standalone dashboard — pure HTML/JS, reads proxy API
  serve.py             HTTP server for remote nginx deployment
scripts/
  deploy.py            Full deploy to DGX Spark (build + restart)
  setup-local.ps1      One-time local .venv + dependency setup
  start-proxy-local.ps1  Start proxy locally (RTX 5090)
.github/extensions/
  deploy-dgx/          Extensions for deployment and validation
.env.example           Config template — copy to .env and fill in values
AGENTS.md              This file
TROUBLESHOOTING.md     Common bugs, fixes, and prevention guidelines
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API endpoint |
| `VLLM_BASE_URL` | _(empty)_ | Optional vLLM URL(s), comma-separated |
| `MIN_TEMPERATURE` | `0.6` | Temperature floor for Qwen3 compatibility |
| `DISABLE_THINKING_FOR_TOOLS` | `true` | Suppress thinking chains by default |
| `ROUTER_REFRESH_S` | `30` | Seconds between backend re-discovery polls |
| `MONGO_URI` | _(empty)_ | MongoDB connection string (optional) |
| `MONGO_DB` | `radiacode` | MongoDB database name |

---

## How Model Routing Works

1. VS Code sends `POST /v1/chat/completions` with `"model": "qwen3"`
2. `BackendRouter.get_backend("qwen3")` checks its registry
3. Registry is built by polling `GET {OLLAMA_BASE_URL}/api/tags` every 30s
4. If found in Ollama registry: route to `{ollama_url}/v1/chat/completions`
5. If found in vLLM registry (if configured): route to vLLM
6. If not found anywhere: route to Ollama anyway (auto-loads from disk on first hit)
7. Temperature clamped to >= 0.6; thinking suppressed unless client opts in

**Model name matching:**
- Exact: `"qwen3:latest"` matches `qwen3:latest`
- Base alias: `"qwen3"` automatically matches `qwen3:latest`
- Unknown model: falls through to Ollama (fails gracefully if not downloaded)

---

## Thinking Mode

The proxy suppresses thinking chains by default (`reasoning_effort=none`). This prevents:
- Heavy think chains (100+ seconds) that break tool-calling
- Empty responses caused by Ollama stripping `<think>` blocks

To enable thinking for a request, the client must explicitly send:
- `"reasoning_effort": "auto"` (or `"high"`, `"low"`)
- Or `"thinking": {"type": "enabled"}` (Anthropic-style)

VS Code with `"thinking": true` in `chatLanguageModels.json` will send `reasoning_effort=auto`
automatically, enabling thinking for that model entry.

---

## Proxy Restart Protocol

The VS Code Copilot session runs THROUGH this proxy. Restarting it terminates the current session.

**Rules:**
1. Make ALL code changes before issuing any restart command
2. Use `python scripts/deploy.py` — builds and restarts atomically on the DGX
3. Expect session termination — VS Code reconnects automatically on next message
4. Never restart mid-task — only as the final action after all changes are complete

---

## Completion Report Format

Before reporting task completion, provide:
1. **What changed** — which files were modified and why
2. **Validation commands** — exact commands run, in order
3. **Validation results** — pass/fail for each, with relevant output
4. **Assumptions** — anything inferred rather than explicitly stated
5. **Remaining limitations** — if any exist after completion

## Prohibited Shortcuts
- Do not claim completion without running validation commands
- Do not suppress errors, remove assertions, or disable checks to get a passing result
- Do not hardcode model names, IPs, or credentials in proxy code
- Do not invent API signatures — read source files first
