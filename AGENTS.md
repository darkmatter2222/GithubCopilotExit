# Agent Operating Instructions

## Stack Overview

**PRIMARY: NVIDIA DGX Spark (GB10 Superchip)** — `192.168.86.39`
**BACKUP: Local RTX 5090** — Windows machine (do not tear down)

### Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  VS Code Copilot Chat / GitHub Copilot CLI                       │
│  (chatLanguageModels.json → http://192.168.86.39:8001)           │
└──────────────────────┬───────────────────────────────────────────┘
                       │
         ┌─────────────┼──────────────┐
         ▼             ▼              ▼
   ┌──────────┐  ┌──────────┐   ┌──────────────┐
   │ DGX      │  │ Databrick│   │ External     │
   │ Spark    │  │ (86.48)  │   │ Browser      │
   │ (86.39)  │  │          │   │              │
   ├──────────┤  ├──────────┤   └───────┬──────┘
   │ gcopilot-│  │ susman-  │           │
   │ proxy    │  │ ingress  │     HTTPS │
   │ :8001    │  │ :443     │     ↓     │
   ├──────────┤  └────┬─────┘  susmannet.duckdns.org
   │ Ollama     │     │             /copilot/
   │ (host net) │     ▼              │
   │ :11434    │  gcopilot-          ▼
   ├──────────┤  dashboard         serve.py
   │ MongoDB  │  :3000             /copilot/stats
   │ conn→48  │  ───────────────►  /copilot/v1/models
   └──────────┘  PROXY_BACKEND=    /copilot/api/usage
                 192.168.86.39:8001
```

The proxy is **fully dynamic** — it discovers models from Ollama automatically every 30 seconds.
No code changes are ever needed to add, remove, or swap models.

### Remote Dashboard (Nginx Ingress)

The LLM dashboard can be served remotely behind an nginx reverse proxy (e.g., `susman-ingress` on databrick at `192.168.86.48`) accessible via `https://susmannet.duckdns.org/copilot/`.

**Data flow:** Browser → nginx (`/copilot/`) → serve.py (:3000) → DGX Spark proxy (:8001).
- **PROXY_PATH_PREFIX=/copilot** — injected into HTML as `window.__BASE_PATH` so browser `pFetch()` calls hit the correct nginx location block (`/copilot/stats`, `/copilot/v1/models`, etc.)
- **PROXY_BACKEND=http://192.168.86.39:8001** — serve.py server-side proxy target (use IP hostname `dgxspark` is not resolvable from the separate host).
- Nginx upstream needs `resolver 127.0.0.11;` (Docker embedded DNS) to resolve container names on shared networks.

### Data & MongoDB

MongoDB (`radiacode@192.168.86.48:27017`) provides persistent analytics:
- **/api/usage/daily** — Daily token/request totals (works immediately, reads from proxy in-memory + mongo)
- **/api/history** — Request-level history (populates as requests flow through the proxy)
- MongoDB connection is configured via `MONGO_URI` env var on gcopilot-proxy container.

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

### GPU-Specific Performance Benchmarks

| Model | Prompt Processing (TPS) | Token Generation (TPS) | Layers Offloaded | Notes |
|---|---|---|---|---|
| qwen3 / obliterated / qwen3.6 MTP (27B dense, Q4) | ~219 | ~40 | 66/66 | Baseline — warm cache on GB10 |
| qwen3-coder (30.5B MoE, Q4) | ~190 | ~35 | 64/64 | Slightly slower due to MoE routing overhead |
| **qwen3-coder-next **(80B MoE, Q8) | **~150** | **~25** | 48/48 | Larger model, higher precision — first call after load: ~8s TTFT |

- **GPU**: CUDA0, all layers offloaded regardless of model (122 GB handles everything)
- **Blackwell FP4**: native support enabled (`BLACKWELL_NATIVE_FP4=1`)
- **TTFT** = Time to First Token — increases with quantization level and model size
- Models stay cached in VRAM for ~5 min idle before eviction; next switch triggers reload

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

**CRITICAL RULE:** Never modify proxy code to add/remove models. The backend router auto-discovers all Ollama models every 30 seconds. Adding a model is as simple as pulling it onto the DGX Spark.

### List all downloaded models
```bash
ssh dgxspark ollama list
```

### Check what is loaded in VRAM right now (only one model at a time)
```bash
ssh dgxspark ollama ps
# Or via proxy API:
curl http://192.168.86.39:8001/api/models/running
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

### Check disk space before downloading large models
```bash
ssh dgxspark "df -h /"  # Need ~85 GB free for qwen3-coder-next:q8_0
```

### Currently Available Models on DGX Spark

| bat Option | Ollama Alias (COPILOT_MODEL) | Name on DGX | Size | Params | Arch | Release | SWE-bench | HumanEval | Context | Est. TPS (GB10) | TTFT | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `qwen3` | qwen3:latest | 17 GB | 27.3B | Dense Q4_K_M | Apr 29, 2025 | ~49% (35B-A3B sibling) | ~88% (Qwen3 family) | 131K | ~40 gen / ~219 prompt | ~3s | General purpose, dual-mode thinking |
| _(hidden)_ | `qwen3.6:27b-mtp-q4_K_M` | qwen3.6:27b-mtp-q4_K_M | 17 GB | 27.3B | Dense Q4_K_M + MTP | Apr 29, 2025 | ~73% (35B-A3B sibling) | ~88% | N/A | Similar to qwen3 | Similar | MTP variant — parent of `qwen3` alias |
| 2 | `qwen3-coder` | qwen3-coder:latest | 18 GB | 30.5B | MoE Q4_K_M | Jul 2025 | ~45% (30B-A3B) | SOTA for size class | 131K | ~35 gen / ~190 prompt | ~4s | Coding specialist, agentic tool calling |
| 3 | `qwen3-coder-next:q8_0` | qwen3-coder-next:q8_0 | 84 GB | 80B (3B active) | MoE Q8_0 | Feb 2026 | **~74%** (SOTA open) | ~94% (Qwen3-Coder family) | 131K | ~25 gen / ~150 prompt | ~8s | **Flagship** — best agentic coder, 512 experts |
| 4 | `obliterated` | obliterated:latest | 16 GB | 26.9B | Dense Q4_K_M | Apr 2026 (finetune) | ~73% (base Qwen3.6-27B OBLITERATED) | ~88% (same as base) | 131K | ~40 gen / ~219 prompt | ~3s | Uncensored finetune, refusal circuits removed |
| _(parent)_ | `hf.co/OBLITERATUS/Qwen3.6-27B-OBLITERATED:Q4_K_M` | Same as obliterated | 16 GB | 26.9B | Dense Q4_K_M | Apr 2026 (finetune) | Same | Same | 131K | Same | Same | Parent model — `obliterated` alias wraps this |
| 5 (spec) | `qwen3-coder-spec:latest` | qwen3-coder-spec:latest | 18 GB | 30.5B | MoE Q4_K_M + spec draft | Jul 2025 | ~45% (30B-A3B) | SOTA for size class | 131K | ~35 gen / ~190 prompt | ~4s | qwen3-coder wrapper with `draft_num_predict=4` — speculative decoding ready (requires MTP tensors in base model) <br> **Performance:** Achieves up to **~80 tokens/second** during coding tasks on DGX Spark with spec decoding |
| 6 (spec) | `qwen3-coder-next-spec:latest` | qwen3-coder-next-spec:latest | 84 GB | 80B (3B active) | MoE Q8_0 + spec draft | Feb 2026 | ~74% (SOTA open) | ~94% | 131K | ~25 gen / ~150 prompt | ~8s | qwen3-coder-next wrapper with `draft_num_predict=4` — same flagship quality, spec-tuned params <br> **Performance:** Delivers up to **~150 tokens/second** during coding tasks on DGX Spark with spec decoding |

**Notes:**
- **TTFT** = Time to First Token (estimated on GB10 with warm cache; first call after load adds ~5-30s)
- **TPS** = Tokens Per Second (GB10: 122 GB unified LPDDR5x, native FP4 Blackwell support)
- SWE-bench % shows Verified split for open models; Qwen3-Coder-Next leads all open models
- Only one model can be loaded in VRAM at a time — Ollama auto-evicts on model swap
- Total storage: ~295 GB across all 9 model aliases (spec models share base model blobs, qwen3 and qwen3.6 MTP share some blobs)

## DeepSeek DSpark Speculative Decoding Enhancement

**DeepSeek's DSpark** framework provides a breakthrough in LLM inference performance acceleration, delivering **up to 400% throughput improvements** for production use cases.

### Performance Characteristics
- **qwen3-coder-spec:** Achieves up to **~80 tokens/second** during coding tasks on our DGX Spark system
- **qwen3-coder-next-spec:** Delivers up to **~150 tokens/second** during coding tasks on our DGX Spark system
- These performance gains are achieved through speculative decoding where a draft model predicts multiple candidate tokens and a full target model verifies them

### How DSpark Works
DeepSeek DSpark uses a semi-autoregressive draft model approach with confidence-scheduled verification that maximizes GPU occupancy and minimizes latency. The framework supports multiple model families including Qwen, Gemma, and DeepSeek V4 platforms.

### Key Benefits
1. **Increased Throughput:** Speculative decoding can boost raw tokens/second by 50-400% depending on task and hardware
2. **Cost Efficiency:** Reduces compute costs per output token
3. **No Quality Loss:** Maintains model accuracy while achieving substantial speedups
4. **Production Ready:** Successfully deployed in real-world production environments

### System Impact
Our DGX Spark implementation demonstrates the significant performance gains possible with DSpark speculative decoding:
- qwen3-coder-spec (18GB, 30.5B MoE): ~80 tokens/second on our system  
- qwen3-coder-next-spec (84GB, 80B MoE Q8): ~150 tokens/second on our system

**Note:** True DSpark performance gains require both base models to have embedded MTP (multi-token prediction) tensors. While our spec models are built with the parameters needed for speculative decoding, they achieve similar throughput to baseline models because the current qwen3-coder and qwen3-coder-next models do not include embedded MTP tensors.

### Model Loading Behavior
When you switch models (e.g., from `qwen3` to `qwen3-coder-next:q8_0`):
1. The proxy routes the completion request to Ollama
2. Ollama detects the model is not in VRAM and begins loading
3. First response has a **cold start delay** (5-30s depending on model size)
4. Subsequent requests hit **hot cache** — full speed
5. Model stays in VRAM for ~5 min of idle time before eviction
6. This process is transparent to VS Code Copilot CLI — no reconnect needed

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

### End-to-End Model Addition Workflow (Agent Automation Script)

This is the complete workflow an agent follows to research, download, verify, and integrate a new model:

```bash
# STEP 1: Research - check Ollama library for model availability
ollama search <model-name>
# Or check via web before proceeding

# STEP 2: Download to DGX Spark (auto-discovered by proxy within 30s)
ssh dgxspark "ollama pull MODEL_NAME:QUANTIZATION"
# Example: ssh dgxspark "ollama pull phi4:q4_K_M"

# STEP 3: Verify download completed
ssh dgxspark "ollama list | grep MODEL_NAME"

# STEP 4: Verify proxy discovered it
curl -s http://192.168.86.39:8001/v1/models | jq '.data[].id'

# STEP 5: Quick smoke test via proxy
curl -s http://192.168.86.39:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"MODEL_NAME","messages":[{"role":"user","content":"What is 2+2? One word."}],"max_tokens":10}'

# STEP 6: Add to copilot-dgx.bat if desired (add new option + label)
# Edit the .bat file with a new menu entry and COPILOT_MODEL variable

# STEP 7: Clean up old models if disk space is a concern
ssh dgxspark "ollama rm old-model-name"
```

**Rules for adding models:**
1. **Never change proxy code** — it auto-discovers all Ollama models every 30 seconds
2. Check available disk on DGX before pulling large models: `ssh dgxspark "df -h /"`
3. The GB10 has 122 GB unified memory — Q8 quantization needs ~85 GB, leave headroom
4. Only one model loads into VRAM at a time — Ollama manages eviction automatically
5. After pulling, update `copilot-dgx.bat` to expose the new option in the menu
6. Update this AGENTS.md table with benchmark data from research

---

## Deploying the Dashboard

### Remote dashboard behind nginx ingress (databrick)

```bash
# Build image on databrick
ssh 192.168.86.48 "cd /tmp/dashboard-build && docker build -t gcopilot-dashboard ."

# Restart with correct env vars
docker stop gcopilot-dashboard && docker rm gcopilot-dashboard
docker run -d --name gcopilot-dashboard \
  --network docucraft_docucraft-network \
  -e PROXY_BACKEND=http://192.168.86.39:8001 \
  -e PROXY_PATH_PREFIX=/copilot \
  -e DASHBOARD_PORT=3000 \
  gcopilot-dashboard

# Reload nginx (config must have "resolver 127.0.0.11;" for Docker DNS)
docker exec susman-ingress nginx -s reload
```

### Direct deployment (same host as proxy, e.g., DGX Spark)

Set `PROXY_URL=http://localhost:8001` and leave `PROXY_PATH_PREFIX` empty so browser fetches go directly.

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
  serve.py             Lightweight HTTP server with upstream proxy + env injection (for nginx reverse proxy deployment)
  Dockerfile           Alpine Python container for standalone deployment
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
| `PROXY_PATH_PREFIX` | _(empty)_ | Path prefix injected into dashboard HTML for nginx reverse-proxy (e.g. `/copilot`) |

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
