# GithubCopilotExit

Run a best-in-class local AI coding agent on your RTX 5090 (or 3090) — no GitHub Copilot subscription required.

## What This Sets Up

| Component | Choice | Why |
|---|---|---|
| Local model runtime | [Ollama](https://ollama.com) | Official Qwen3.6 support, RTX 50-series GPU support, simple API |
| Primary coding model | `qwen3.6:35b-a3b-mtp-q4_K_M` | 23 GB, fits RTX 5090, highest agentic-coding benchmark scores of any consumer-GPU model |
| VS Code agent extension | [Roo Code](https://marketplace.visualstudio.com/items?itemName=RooVeterinaryInc.roo-cline) | Full agent loop: reads files, edits, runs terminals, tests, fixes, repeats |
| Autocomplete model | `qwen2.5-coder:1.5b` | Fast, code-specialized, low VRAM cost |

### Why Qwen3.6 35B instead of Gemma 4?

| Model | SWE-bench Verified | Fits RTX 5090 (32 GB) |
|---|---|---|
| Qwen3.6 35B A3B Q4 | **73.4%** | Yes — 23 GB |
| Devstral Small 2 24B | 68.0% | Yes |
| Gemma 4 31B IT Q4 | 52.0% | Yes — 20 GB |
| Qwen3-Coder 80B A3B | Highest | No — 52 GB |

SWE-bench Verified tests agents fixing real repository bugs — the closest public proxy for "agentic VS Code coding."

### Why Roo Code?

Roo Code is the harness that creates the Copilot Agent-style loop:

```
You give it a goal
  → it reads your files
  → edits code
  → runs tests / build
  → reads failures
  → fixes code
  → reruns tests
  → keeps going until done
```

It supports Code Mode, Debug Mode, and Orchestrator Mode, auto-approval of trusted commands, and project-level `.roorules` instruction files.

---

## Quick Start

### Prerequisites
- Windows 10/11
- NVIDIA RTX 5090 (or 3090) with current drivers
- VS Code

### Step 1 — Run the setup script (one time)

```powershell
.\scripts\setup.ps1
```

This will:
1. Install Ollama (if not already installed)
2. Set all required Ollama environment variables
3. Download `qwen3.6:35b-a3b-mtp-q4_K_M` (~23 GB) and `qwen2.5-coder:1.5b`
4. Create custom Ollama model profiles with optimized context settings
5. Install the Roo Code VS Code extension
6. Write the Continue extension config for chat/autocomplete fallback
7. Create `%USERPROFILE%\.qwen\settings.json` for Qwen Code Companion (optional)

### Step 2 — Open any repo in VS Code

Roo Code and the Ollama models are global machine-level installs. Open any project folder.

### Step 3 — Start the Ollama service (if not running)

```powershell
.\scripts\start-ollama.ps1
```

Or just open Ollama from the Start menu — it runs as a system tray service.

### Step 4 — Use Roo Code

1. Click the **Roo Code** icon in the VS Code Activity Bar (left sidebar)
2. Select model: `qwen36-agent-32k` or `qwen36-agent-64k`
3. Select mode: **Code** (normal tasks) or **Orchestrator** (large multi-step builds)
4. Type your request — see [Usage Examples](#usage-examples)

---

## Managing the Model

### Check GPU usage while model is running

```powershell
nvidia-smi -l 1
```

Press `Ctrl+C` to stop the live refresh.

### See what models Ollama has loaded

```powershell
ollama ps
```

You want to see `100% GPU` and `CONTEXT 32768` (or 65536 for the 64K profile).

### List downloaded models

```powershell
ollama list
```

### Stop the model to free VRAM

```powershell
ollama stop qwen36-agent-32k
```

### Unload all models

```powershell
ollama stop $(ollama ps --format "{{.Name}}")
```

### Check Ollama service health

```powershell
Invoke-RestMethod http://localhost:11434/api/tags
```

### Restart Ollama service

Find Ollama in the system tray, right-click → Quit. Then relaunch from the Start menu.  
Or from PowerShell (admin):

```powershell
Stop-Process -Name "ollama" -Force -ErrorAction SilentlyContinue
Start-Sleep 2
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
```

### Update Ollama

```powershell
winget upgrade Ollama.Ollama
```

### Update models

```powershell
ollama pull qwen3.6:35b-a3b-mtp-q4_K_M
ollama pull qwen2.5-coder:1.5b
```

---

## Context Window Management

The model profiles created by `setup.ps1` are:

| Profile | Context | When to use |
|---|---|---|
| `qwen36-agent-32k` | 32 768 tokens | Default — safe, fast, fully GPU-resident |
| `qwen36-agent-64k` | 65 536 tokens | Large codebases — verify `100% GPU` with `ollama ps` first |

### Switch context in Roo Code

Open the Roo Code model picker and select the appropriate profile.

### Increase context further (experimental)

Edit `%USERPROFILE%\ollama-models\Modelfile.qwen36-agent-64k`, change `num_ctx`, then rebuild:

```powershell
cd "$env:USERPROFILE\ollama-models"
ollama create qwen36-agent-64k -f .\Modelfile.qwen36-agent-64k
```

---

## Roo Code Modes

| Mode | Use for |
|---|---|
| **Code** | Feature implementation, refactoring, bug fixes |
| **Debug** | Diagnosing test/build failures |
| **Orchestrator** | Large multi-step builds, multi-file features |
| **Architect** | Design reviews, planning |

Switch modes with the dropdown in the Roo Code panel.

---

## Auto-Approval (let Roo work uninterrupted)

Click the **Auto-Approve** toggle in the Roo Code panel. Start with these enabled:

| Action | Safe to auto-approve |
|---|---|
| Read files | ✅ Yes |
| Search codebase | ✅ Yes |
| Edit existing files | ✅ Yes (after baseline commit) |
| Run formatters / linters / tests | ✅ Yes |
| `git status` / `git diff` / `git log` | ✅ Yes |

Keep these requiring confirmation until you trust the model on your codebase:

| Action | Require confirmation |
|---|---|
| Delete files | ⚠️ Confirm |
| Install packages | ⚠️ Confirm |
| Push / deploy | ⚠️ Confirm |
| Modify secrets/env files | ⚠️ Confirm |

**Always commit your work to Git before enabling auto-approval for file edits.**

```powershell
git switch -c local-ai-agent-session
git add -A
git commit -m "Baseline before AI agent session"
```

---

## Usage Examples

### Implement a feature end to end

```
Implement CSV export for the reporting module.
- Add the endpoint / function
- Preserve existing auth and filtering
- Add or update tests
- Run the test suite and fix all failures before stopping
```

### Debug a failing test

```
Tests are failing with this error: [paste error]
Find the root cause, fix the code (not the test), rerun the tests, and continue until they pass.
```

### Refactor

```
Refactor the authentication module into smaller testable functions.
Preserve all existing behavior. Update tests. Run them.
```

### Large build (Orchestrator mode)

```
Build a REST API with CRUD endpoints for a User entity.
Include validation, error handling, and tests.
Run the build and all tests. Fix failures. Do not stop until the build passes.
```

---

## Project-Level Instructions (.roorules)

Each repository can have a `.roorules` file at its root that gives Roo Code project-specific context. A template is in [`templates/.roorules`](templates/.roorules). Copy it to your repo root and fill in the commands.

---

## AGENTS.md

[`AGENTS.md`](AGENTS.md) defines the agent's operating rules for this repository specifically. Copy it to any repository where you want to use this stack with consistent behavior.

---

## Troubleshooting

### Model is using CPU instead of GPU

```powershell
ollama ps
```

If you see CPU % > 0%, your context is too large for available VRAM. Switch to the 32K profile:

In Roo Code, change the model to `qwen36-agent-32k`.

### Roo Code says "model not found"

Make sure Ollama is running (check system tray) and the profile was created:

```powershell
ollama list
```

If `qwen36-agent-32k` is missing, rerun:

```powershell
.\scripts\setup.ps1
```

### Ollama not starting

Check Windows Defender Firewall — allow Ollama on local network (port 11434).

### Model feels slow

- Make sure no other GPU-heavy applications are running (games, other AI tools)
- Check VRAM pressure: `nvidia-smi`
- Confirm `OLLAMA_FLASH_ATTENTION=1` is set: `[System.Environment]::GetEnvironmentVariable("OLLAMA_FLASH_ATTENTION","User")`

---

## Architecture

```
VS Code
└── Roo Code extension (agent harness)
    └── Ollama API (http://localhost:11434)
        └── qwen36-agent-32k or qwen36-agent-64k
            └── qwen3.6:35b-a3b-mtp-q4_K_M base model
                └── NVIDIA RTX 5090 (32 GB GDDR7)

VS Code (autocomplete)
└── Continue extension
    └── Ollama API
        └── qwen2.5-coder:1.5b (fast, low VRAM)
```

---

## Files in This Repo

| File | Purpose |
|---|---|
| `scripts/setup.ps1` | One-shot machine setup script |
| `scripts/start-ollama.ps1` | Start/verify Ollama service |
| `scripts/check-gpu.ps1` | GPU health and model residency check |
| `AGENTS.md` | Agent operating rules (copy to any repo) |
| `templates/.roorules` | Per-repo instruction template (copy to your repo root) |
| `templates/continue-config.yaml` | Continue extension config template |
| `templates/qwen-settings.json` | Qwen Code Companion settings template |
