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

**Remote server specs:** RTX 3090 24 GB VRAM, running Ubuntu, Docker with nvidia-container-toolkit.

**Model:** `qwen3.6:27b-mtp-q4_K_M` (~18 GB, Q4_K_M quantization, 262K context, native tool calling, vision, thinking mode).

**Proxy:** FastAPI on port 8001 — clamps temperature to ≥ 0.6 before forwarding to Ollama on `localhost:11434`.

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
- `ollama/ollama:latest` is too old — does not know `qwen35` architecture, throws `unknown model architecture: 'qwen35'`
- `ollama/ollama:updated` (custom build) — broke inference, `llama-server binary not found`
- `ollama/ollama:v0.30.8-final` — ✅ working

### qwen3 alias (must be recreated after every `docker rm`)

The model is served as `qwen3` not `qwen3.6:27b-mtp-q4_K_M`. The alias bakes in `num_ctx 262144`. Without it the model runs with 32K context, causing `finish_reason: length` errors on long sessions.

```bash
docker exec ollama bash -c '
printf "FROM qwen3.6:27b-mtp-q4_K_M\nPARAMETER num_ctx 262144\n" > /tmp/qwen3.modelfile
ollama create qwen3 -f /tmp/qwen3.modelfile
'
```

### Proxy container

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

## Objective

Implement requested changes completely within this repository. A task is **not complete** when code has merely been written. It is complete only when the requested behavior is implemented and the applicable validation succeeds, or when a concrete, unrecoverable blocker is explicitly documented with evidence.

## Required Working Pattern

### 1. Inspect before changing

- Read the relevant source files, tests, configuration, and nearby implementation patterns before writing any code.
- Identify the repository's established conventions (naming, error handling, test structure, dependency patterns) and follow them unless the task explicitly requires a change.
- Do not assume what a file contains — read it.

### 2. Implement coherently

- Make the smallest coherent set of changes that fully satisfies the requested outcome.
- Preserve all existing behavior outside the requested scope.
- Do not rewrite unrelated components or introduce new dependencies unless necessary and justified.
- Do not generate placeholder implementations. Implement the real behavior.

### 3. Validate after every change

- Run the narrowest relevant tests first (unit tests for the modified module).
- Then run the full applicable chain: formatter → linter → type checker → build → integration tests.
- Use the commands listed in the **Repository Commands** section below.
- When validation fails because of a change, diagnose the actual root cause, apply a corrective fix, and rerun validation. Do not suppress, skip, or weaken tests to obtain a passing result.

### 4. Continue until done

- Do not stop after producing a plan, outline, partial patch, or explanation.
- Do not report success while any relevant test, type check, or build step fails.
- Continue the inspect → implement → validate → fix loop until validation succeeds or a specific blocker is proven with evidence.
- "I believe this should work" is not completion. Running the validation and seeing it pass is completion.

### 5. Protect the repository

- Operate only in the assigned branch or isolated worktree.
- Do not commit, push, publish, deploy, change credentials, delete significant files, or modify infrastructure without explicit instruction from the user.
- Do not weaken tests, remove assertions, or disable linting rules to obtain a passing run.
- Do not introduce hardcoded credentials, secrets, insecure defaults, or SQL/command injection vectors.
- Never commit `.env` or any file containing real IPs, usernames, SSH key paths, or passwords.

## Repository Commands

```
Install dependencies : pip install -r proxy/requirements.txt
Smoke test proxy    : python scripts/test-proxy.py
Deploy to remote    : bash scripts/deploy-remote.sh
Fix Ollama ctx      : bash scripts/fix-context.sh  (run on remote)
Warm up model       : python scripts/warmup.py     (run on remote)
Build               : NOT APPLICABLE (no compiled artifacts)
Unit tests          : NOT APPLICABLE (integration-only)
```

## Architecture and Conventions

- **Primary language/framework:** Python 3.12, FastAPI, httpx, uvicorn (proxy); Bash + PowerShell (ops scripts)
- **Preferred testing pattern:** Smoke tests via `scripts/test-proxy.py` (no unit test framework)
- **Error handling convention:** Proxy passes Ollama error responses through unchanged with original HTTP status codes
- **Logging convention:** `logging.basicConfig` to stdout, INFO level, timestamped
- **Dependency management:** `pip` with pinned versions in `proxy/requirements.txt`
- **Security/privacy constraints:** `.env` is gitignored and must never be committed. No auth on the proxy (LAN-only service). No hardcoded IPs or usernames in committed files.

## Completion Report

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


## Objective

Implement requested changes completely within this repository. A task is **not complete** when code has merely been written. It is complete only when the requested behavior is implemented and the applicable validation succeeds, or when a concrete, unrecoverable blocker is explicitly documented with evidence.

## Required Working Pattern

### 1. Inspect before changing

- Read the relevant source files, tests, configuration, and nearby implementation patterns before writing any code.
- Identify the repository's established conventions (naming, error handling, test structure, dependency patterns) and follow them unless the task explicitly requires a change.
- Do not assume what a file contains — read it.

### 2. Implement coherently

- Make the smallest coherent set of changes that fully satisfies the requested outcome.
- Preserve all existing behavior outside the requested scope.
- Do not rewrite unrelated components or introduce new dependencies unless necessary and justified.
- Do not generate placeholder implementations. Implement the real behavior.

### 3. Validate after every change

- Run the narrowest relevant tests first (unit tests for the modified module).
- Then run the full applicable chain: formatter → linter → type checker → build → integration tests.
- Use the commands listed in the **Repository Commands** section below.
- When validation fails because of a change, diagnose the actual root cause, apply a corrective fix, and rerun validation. Do not suppress, skip, or weaken tests to obtain a passing result.

### 4. Continue until done

- Do not stop after producing a plan, outline, partial patch, or explanation.
- Do not report success while any relevant test, type check, or build step fails.
- Continue the inspect → implement → validate → fix loop until validation succeeds or a specific blocker is proven with evidence.
- "I believe this should work" is not completion. Running the validation and seeing it pass is completion.

### 5. Protect the repository

- Operate only in the assigned branch or isolated worktree.
- Do not commit, push, publish, deploy, change credentials, delete significant files, or modify infrastructure without explicit instruction from the user.
- Do not weaken tests, remove assertions, or disable linting rules to obtain a passing run.
- Do not introduce hardcoded credentials, secrets, insecure defaults, or SQL/command injection vectors.

## Completion Report

Before reporting completion, provide all of the following:

1. **What changed** — which files were modified and why.
2. **Validation commands executed** — exact commands run, in order.
3. **Validation results** — pass/fail for each command, with any relevant output.
4. **Assumptions made** — anything inferred rather than explicitly stated.
5. **Remaining limitations or known issues** — if any exist after completion.

## Repository Commands

> Fill these in for each project. Run `./scripts/setup.ps1` or your project's bootstrap command first.

```
Install dependencies : [COMMAND]
Format               : [COMMAND]
Lint                 : [COMMAND]
Type check           : [COMMAND]
Unit tests           : [COMMAND]
Integration tests    : [COMMAND or NOT APPLICABLE]
Build                : [COMMAND]
```

**Common examples by ecosystem:**

| Ecosystem | Test command | Build command |
|---|---|---|
| Python | `python -m pytest` | `python -m build` |
| Node/TypeScript | `npm test` | `npm run build` |
| .NET | `dotnet test` | `dotnet build` |
| Rust | `cargo test` | `cargo build` |
| Go | `go test ./...` | `go build ./...` |

## Architecture and Conventions

> Fill in project-specific rules. Generate a starting point with `/init` in Qwen Code or Roo Code.

- **Primary language/framework:** `[VALUE]`
- **Preferred testing pattern:** `[VALUE]`
- **Error handling convention:** `[VALUE]`
- **Logging convention:** `[VALUE]`
- **Dependency management:** `[VALUE]`
- **Security/privacy constraints:** `[VALUE]`

## Prohibited Shortcuts

- Do not claim completion without executing the relevant validation commands.
- Do not replace working implementation patterns with untested speculative abstractions.
- Do not suppress errors, remove assertions, disable tests, comment out checks, or weaken validation to obtain a passing run.
- Do not introduce secrets, hardcoded credentials, insecure defaults, or unrelated formatting churn.
- Do not modify files outside the scope of the requested change without explicit justification.
- Do not invent API signatures, function names, or module paths that do not exist — read the actual source first.
