<#
.SYNOPSIS
    GithubCopilotExit — One-shot machine setup.
    Run this once on any Windows machine with an RTX 3090 or 5090.

.DESCRIPTION
    1. Installs Ollama (if not present)
    2. Sets all required Ollama environment variables
    3. Downloads qwen3.6:35b-a3b-mtp-q4_K_M and qwen2.5-coder:1.5b
    4. Creates optimized Ollama model profiles (32K and 64K context)
    5. Installs the Roo Code VS Code extension
    6. Writes Continue extension config (~/.continue/config.yaml)
    7. Writes Qwen Code Companion settings (~/.qwen/settings.json)

.NOTES
    Requires: Windows 10/11, NVIDIA RTX GPU, VS Code, winget
    Run from any directory — paths are absolute.
#>

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-OK([string]$Message) {
    Write-Host "    OK: $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "    WARN: $Message" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 0. Check GPU
# ---------------------------------------------------------------------------
Write-Step "Checking GPU"
$gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warn "nvidia-smi not found or failed. Make sure NVIDIA drivers are installed."
    Write-Host "    Detected: $gpuInfo"
} else {
    Write-OK "GPU: $gpuInfo"
}

# ---------------------------------------------------------------------------
# 1. Install Ollama
# ---------------------------------------------------------------------------
Write-Step "Checking Ollama installation"
$ollamaPath = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollamaPath) {
    Write-Host "    Ollama not found — installing via winget..."
    winget install Ollama.Ollama --accept-package-agreements --accept-source-agreements
    # Refresh PATH
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH","User")
} else {
    Write-OK "Ollama already installed: $(ollama --version)"
}

# Confirm it's reachable
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH","User")
$version = ollama --version 2>&1
Write-OK "Ollama version: $version"

# ---------------------------------------------------------------------------
# 2. Set Ollama environment variables
# ---------------------------------------------------------------------------
Write-Step "Setting Ollama environment variables"

$envVars = @{
    "OLLAMA_FLASH_ATTENTION"  = "1"
    "OLLAMA_KV_CACHE_TYPE"    = "q8_0"
    "OLLAMA_NUM_PARALLEL"     = "1"
    "OLLAMA_MAX_LOADED_MODELS"= "1"
    "OLLAMA_KEEP_ALIVE"       = "24h"
    "OLLAMA_API_KEY"          = "ollama-local"
}

foreach ($key in $envVars.Keys) {
    [Environment]::SetEnvironmentVariable($key, $envVars[$key], "User")
    # Also set in current session
    Set-Item -Path "env:$key" -Value $envVars[$key]
    Write-OK "$key = $($envVars[$key])"
}

Write-Warn "NOTE: Close and reopen any terminal after setup for env vars to take full effect."
Write-Warn "      Restart Ollama from the system tray after setting env vars."

# ---------------------------------------------------------------------------
# 3. Start Ollama service (best effort — may already be running)
# ---------------------------------------------------------------------------
Write-Step "Starting Ollama service"
$ollamaProcess = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
if (-not $ollamaProcess) {
    Write-Host "    Launching Ollama..."
    $ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $ollamaExe) {
        Start-Process $ollamaExe -WindowStyle Hidden
        Start-Sleep -Seconds 4
        Write-OK "Ollama started"
    } else {
        Write-Warn "Could not find ollama.exe at $ollamaExe. Launch Ollama from the Start menu."
    }
} else {
    Write-OK "Ollama is already running (PID $($ollamaProcess.Id))"
    Write-Warn "Restart Ollama from the system tray so new env vars take effect."
}

# ---------------------------------------------------------------------------
# 4. Pull models
# ---------------------------------------------------------------------------
Write-Step "Pulling primary model: qwen3.6:35b-a3b-mtp-q4_K_M (~23 GB)"
Write-Host "    This will take several minutes on first run."
Write-Host "    Subsequent runs skip the download if model is current."
ollama pull qwen3.6:35b-a3b-mtp-q4_K_M

Write-Step "Pulling autocomplete model: qwen2.5-coder:1.5b (~1 GB)"
ollama pull qwen2.5-coder:1.5b

Write-OK "Models downloaded"
ollama list

# ---------------------------------------------------------------------------
# 5. Create Ollama model profiles
# ---------------------------------------------------------------------------
Write-Step "Creating Ollama model profiles"
$modelDir = "$env:USERPROFILE\ollama-models"
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null

# 32K profile (default — safe for all tasks)
$modelfile32k = @"
FROM qwen3.6:35b-a3b-mtp-q4_K_M
PARAMETER num_ctx 32768
PARAMETER temperature 1
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER num_predict 8192
"@
$modelfile32k | Set-Content -Encoding UTF8 "$modelDir\Modelfile.qwen36-agent-32k"
ollama create qwen36-agent-32k -f "$modelDir\Modelfile.qwen36-agent-32k"
Write-OK "Created profile: qwen36-agent-32k (32K context)"

# 64K profile (extended — verify 100% GPU with `ollama ps` before using)
$modelfile64k = @"
FROM qwen3.6:35b-a3b-mtp-q4_K_M
PARAMETER num_ctx 65536
PARAMETER temperature 1
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER num_predict 8192
"@
$modelfile64k | Set-Content -Encoding UTF8 "$modelDir\Modelfile.qwen36-agent-64k"
ollama create qwen36-agent-64k -f "$modelDir\Modelfile.qwen36-agent-64k"
Write-OK "Created profile: qwen36-agent-64k (64K context — verify GPU residency before using)"

# ---------------------------------------------------------------------------
# 6. Install VS Code extensions
# ---------------------------------------------------------------------------
Write-Step "Installing VS Code extensions"

$extensions = @(
    "RooVeterinaryInc.roo-cline",    # Roo Code — primary agent harness
    "continue.continue"               # Continue — chat/autocomplete fallback
)

foreach ($ext in $extensions) {
    Write-Host "    Installing $ext..."
    code --install-extension $ext --force 2>&1 | Out-Null
    Write-OK "Installed: $ext"
}

# ---------------------------------------------------------------------------
# 7. Write Continue config
# ---------------------------------------------------------------------------
Write-Step "Writing Continue extension config"
$continueDir = "$env:USERPROFILE\.continue"
New-Item -ItemType Directory -Path $continueDir -Force | Out-Null

$continueConfig = @"
# Continue Extension Configuration — Local Qwen3.6 on RTX 5090
# Written by GithubCopilotExit setup.ps1
name: Local Qwen3.6 - RTX 5090
version: 0.0.1
schema: v1

models:
  - name: Qwen3.6 35B A3B MTP (Local RTX 5090)
    provider: ollama
    model: qwen36-agent-32k
    apiBase: http://localhost:11434
    roles:
      - chat
      - edit
      - apply
    capabilities:
      - tool_use
    defaultCompletionOptions:
      contextLength: 32768
      temperature: 1.0
      top_p: 0.95
      top_k: 20
      num_predict: 8192

  - name: Qwen3.6 35B A3B MTP 64K (Local RTX 5090)
    provider: ollama
    model: qwen36-agent-64k
    apiBase: http://localhost:11434
    roles:
      - chat
      - edit
      - apply
    capabilities:
      - tool_use
    defaultCompletionOptions:
      contextLength: 65536
      temperature: 1.0
      top_p: 0.95
      top_k: 20
      num_predict: 8192

  - name: Fast Autocomplete (qwen2.5-coder 1.5B)
    provider: ollama
    model: qwen2.5-coder:1.5b
    apiBase: http://localhost:11434
    roles:
      - autocomplete
"@

$continueConfig | Set-Content -Encoding UTF8 "$continueDir\config.yaml"
Write-OK "Written: $continueDir\config.yaml"

# ---------------------------------------------------------------------------
# 8. Write Qwen Code Companion settings (optional extension)
# ---------------------------------------------------------------------------
Write-Step "Writing Qwen Code Companion settings"
$qwenDir = "$env:USERPROFILE\.qwen"
New-Item -ItemType Directory -Path $qwenDir -Force | Out-Null

$qwenSettings = @"
{
  "env": {
    "OLLAMA_API_KEY": "ollama-local"
  },
  "modelProviders": {
    "openai": [
      {
        "id": "qwen36-agent-32k",
        "name": "Qwen3.6 35B A3B MTP - Local RTX 5090 (32K)",
        "description": "Primary agentic coding model via Ollama.",
        "envKey": "OLLAMA_API_KEY",
        "baseUrl": "http://localhost:11434/v1",
        "generationConfig": {
          "contextWindowSize": 32768,
          "timeout": 600000,
          "maxRetries": 2,
          "samplingParams": {
            "temperature": 1,
            "max_tokens": 8192
          }
        }
      },
      {
        "id": "qwen36-agent-64k",
        "name": "Qwen3.6 35B A3B MTP - Local RTX 5090 (64K)",
        "description": "Extended context. Verify 100% GPU with ollama ps before using.",
        "envKey": "OLLAMA_API_KEY",
        "baseUrl": "http://localhost:11434/v1",
        "generationConfig": {
          "contextWindowSize": 65536,
          "timeout": 900000,
          "maxRetries": 2,
          "samplingParams": {
            "temperature": 1,
            "max_tokens": 8192
          }
        }
      }
    ]
  },
  "security": {
    "auth": {
      "selectedType": "openai"
    }
  },
  "model": {
    "name": "qwen36-agent-32k"
  }
}
"@

$qwenSettings | Set-Content -Encoding UTF8 "$qwenDir\settings.json"
Write-OK "Written: $qwenDir\settings.json"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host "`n" + ("=" * 70) -ForegroundColor Green
Write-Host "  SETUP COMPLETE" -ForegroundColor Green
Write-Host ("=" * 70) -ForegroundColor Green
Write-Host @"

Next steps:
  1. Restart Ollama from the system tray (so new env vars take effect)
  2. Open any repository in VS Code
  3. Click the Roo Code icon in the Activity Bar
  4. Set model to: qwen36-agent-32k
  5. Set mode to: Code (or Orchestrator for large tasks)
  6. Create a git branch before your first auto-approved session:
       git switch -c ai-agent-session
       git add -A && git commit -m "Baseline"

Verify GPU residency after starting a task:
  ollama ps        (want: 100% GPU, CONTEXT 32768)
  nvidia-smi       (want: ~25-28 GB VRAM used)

See README.md for full usage guide.
"@
