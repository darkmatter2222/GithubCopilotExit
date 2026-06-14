# Agent Operating Instructions

## Infrastructure Access

All server credentials are stored in `.env` (gitignored). **Do not prompt the user for credentials** — read `.env` instead. Never commit `.env` or any file containing real IPs, usernames, or key paths.

| Variable | Description |
|---|---|
| `SSH_USER` | Linux username on the remote GPU server |
| `SSH_HOST` | LAN IP address of the remote GPU server |
| `SSH_KEY_PATH` | Path to the SSH private key (default: `~/.ssh/id_rsa`) |
| `REMOTE_PATH` | Deploy directory on the remote server |
| `API_PORT` | Port the temperature proxy listens on (default: 8001) |
| `OLLAMA_MODEL` | Ollama model tag (default: `qwen3.6:27b-mtp-q4_K_M`) |
| `SERVED_MODEL_NAME` | Model alias name served to clients (default: `qwen3`) |
| `MIN_TEMPERATURE` | Minimum temperature floor enforced by the proxy (default: `0.6`) |

**Remote server specs:** RTX 3090, 24 GB VRAM, running Ubuntu, Docker with nvidia-container-toolkit.

**Model:** `qwen3.6:27b-mtp-q4_K_M` (~18 GB, Q4\_K\_M quantization, 262K context, native tool calling, vision, thinking mode).

**Proxy:** FastAPI on port 8001 — clamps temperature to ≥ 0.6 before forwarding to Ollama. Exposes live stats dashboard at `/dashboard`.

To SSH in: `ssh -i $SSH_KEY_PATH $SSH_USER@$SSH_HOST`

---

## Stack State (as of June 2026)

This section documents the exact running state so you can restore it from scratch.

### Ollama container

```bash
docker run -d \
  --name ollama \
  --gpus all \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  --restart unless-stopped \
  -e OLLAMA_KEEP_ALIVE=-1 \
  -e OLLAMA_NUM_CTX=262144 \
  ollama/ollama:v0.30.8-final
```

**Critical image version note:** Use `v0.30.8-final` specifically.
- `ollama/ollama:latest` — too old, unknown model architecture for qwen35
- `ollama/ollama:updated` — broke inference, llama-server binary not found
- `ollama/ollama:v0.30.8-final` — working, supports qwen3.6 and 262K context

### qwen3 alias (must be recreated after every Docker restart)

The model is served as `qwen3` not the full `qwen3.6:27b-mtp-q4_K_M`. The alias bakes in `num_ctx 262144`. Without it the model defaults to 32K context, causing `finish_reason: length` errors on long sessions.

```bash
docker exec ollama bash -c '
printf "FROM qwen3.6:27b-mtp-q4_K_M\nPARAMETER num_ctx 262144\n" > /tmp/qwen3.modelfile
ollama create qwen3 -f /tmp/qwen3.modelfile
'
```

### Proxy container

Runs on host network + FastAPI with token tracker. Tracks throughput in-memory (no DB).

```bash
docker run -d \
  --name llm-proxy \
  --network host \
  --restart unless-stopped \
  -e OLLAMA_BASE_URL=http://localhost:11434 \
  -e SERVED_MODEL_NAME=qwen3 \
  -e MIN_TEMPERATURE=0.6 \
  llm-proxy:latest
```

### Proxy Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat (VS Code talks here) |
| `GET  /health` | Health check — proxies to Ollama status |
| `GET  /dashboard` | **LIVE dashboard** — dark-themed HTML, auto-refreshes every 2s |

### VS Code client config (`%APPDATA%\Code\User\chatLanguageModels.json`)

```json
{
  "name": "Local RTX 3090",
  "vendor": "customendpoint",
  "apiKey": "no-key",
  "apiType": "chat-completions",
  "models": [{
    "id": "qwen3",
    "name": "Qwen3.6-27B (RTX 3090)",
    "url": "http://<SSH_HOST>:8001/v1/chat/completions",
    "toolCalling": true,
    "vision": true,
    "maxInputTokens": 131072,
    "maxOutputTokens": 32000,
    "thinking": true,
    "streaming": true
  }]
}
```

---

## Recovery Runbook

If the stack breaks and you need to restore it from scratch:

```bash
# 1. SSH to the server
ssh -i $SSH_KEY_PATH $SSH_USER@$SSH_HOST

# 2. Restore Ollama with correct image + env vars + create qwen3 alias + warm VRAM
bash /tmp/fix-context.sh       # if script already on server
# or from repo:
scp -i $SSH_KEY_PATH scripts/fix-context.sh $SSH_USER@$SSH_HOST:/tmp/fix-context.sh
scp -i $SSH_KEY_PATH scripts/warmup.py $SSH_USER@$SSH_HOST:/tmp/warmup.py
ssh -i $SSH_KEY_PATH $SSH_USER@$SSH_HOST "bash /tmp/fix-context.sh"

# 3. Rebuild and restart the proxy
bash scripts/deploy-remote.sh

# 4. Verify
ssh -i $SSH_KEY_PATH $SSH_USER@$SSH_HOST "curl -s http://localhost:8001/health"
```

**Verify the context length is 262K (not 32K):**
```bash
ssh -i $SSH_KEY_PATH $SSH_USER@$SSH_HOST "curl -s http://localhost:11434/api/ps"
# Look for: "context_length":262144
# If it shows 32768, run fix-context.sh again
```

---

## Known Issues Reference

| Error | Root Cause | Fix |
|---|---|---|
| `ERR_INCOMPLETE_CHUNKED_ENCODING` at exactly 5 min | `httpx` timeout was 300s in proxy | `timeout=None` in `proxy/main.py` — already fixed |
| `finish_reason: length` / Response too long | `qwen3` alias has 32K not 262K context | Run `fix-context.sh` |
| `unknown model architecture: 'qwen35'` | Wrong Ollama Docker image | Use `v0.30.8-final` |
| `llama-server binary not found` | `:updated` custom image broke inference engine | Use `v0.30.8-final` |
| Model not in VRAM on first request | `OLLAMA_KEEP_ALIVE` not set or container recreated | Set `-e OLLAMA_KEEP_ALIVE=-1`, run `warmup.py` |
| `maxOutputTokens` error in VS Code | Cap too low for thinking mode (5K–15K tokens burned on `<think>`) | Set to 32000 in `chatLanguageModels.json` |

---

## Repository Commands

```
Install dependencies : pip install -r proxy/requirements.txt
Smoke test proxy     : python scripts/test-proxy.py
Deploy to remote     : bash scripts/deploy-remote.sh
Fix Ollama ctx       : bash scripts/fix-context.sh  (run on remote)
Warm up model        : python scripts/warmup.py     (run on remote)
Build                : NOT APPLICABLE (no compiled artifacts)
Unit tests           : NOT APPLICABLE (integration-only)
```

## Architecture and Conventions

- **Primary language/framework:** Python 3.12, FastAPI, httpx, uvicorn (proxy); Bash + PowerShell (ops scripts)
- **Preferred testing pattern:** Smoke tests via `scripts/test-proxy.py` (no unit test framework)
- **Error handling convention:** Proxy passes Ollama error responses through unchanged with original HTTP status codes
- **Logging convention:** `logging.basicConfig` to stdout, INFO level, timestamped
- **Dependency management:** `pip` with pinned versions in `proxy/requirements.txt`
- **Security/privacy constraints:** `.env` is gitignored and must never be committed. No auth on the proxy (LAN-only service). No hardcoded IPs or usernames in committed files.

### Key Files

| File | Purpose |
|---|---|
| `proxy/main.py` | FastAPI proxy app — routes, temp clamping, streaming handler, dashboard HTML |
| `proxy/tracker.py` | Thread-safe real-time token throughput tracker (in-memory, no DB) |
| `proxy/Dockerfile` | Container image for proxy (`COPY *.py ./` to include all Python files) |
| `scripts/deploy-remote.sh` | One-command deploy: scp source + build image + start containers |
| `scripts/fix-context.sh` | Full Ollama recovery: restart container, recreate qwen3 alias, warm VRAM |
| `scripts/start-proxy-local.ps1` | Run proxy locally on Windows (for dev/testing without Docker) |
| `scripts/test-proxy.py` | Smoke test — hits /health and /v1/chat/completions endpoints |
| `scripts/warmup.py` | Load model into VRAM after startup (prevents cold-first-request lag) |

## Completion Report Format

Before reporting completion, provide all of the following:

1. **What changed** — which files were modified and why.
2. **Validation commands executed** — exact commands run, in order.
3. **Validation results** — pass/fail for each command, with any relevant output.
4. **Assumptions made** — anything inferred rather than explicitly stated.
5. **Remaining limitations or known issues** — if any exist after completion.

## Prohibited Shortcuts

- Do not claim completion without executing the relevant validation commands.
- Do not replace working implementation patterns with untested speculative abstractions.
- Do not suppress errors, remove assertions, disable tests, comment out checks, or weaken validation to obtain a passing run.
- Do not introduce secrets, hardcoded credentials, insecure defaults, or unrelated formatting churn.
- Do not modify files outside the scope of the requested change without explicit justification.
- Do not invent API signatures, function names, or module paths that do not exist — read the actual source first.
