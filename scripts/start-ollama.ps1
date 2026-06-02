<#
.SYNOPSIS
    Start and verify the Ollama service.
    Run this at the beginning of any AI agent coding session.
#>

$ErrorActionPreference = "SilentlyContinue"

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "    WARN: $msg" -ForegroundColor Yellow }
function Write-Fail([string]$msg) { Write-Host "    FAIL: $msg" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 1. Ensure Ollama process is running
# ---------------------------------------------------------------------------
Write-Step "Checking Ollama service"
$proc = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "    Ollama not running — starting..."
    $ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $ollamaExe) {
        Start-Process $ollamaExe -WindowStyle Hidden
        Write-Host "    Waiting for service to be ready..."
        $ready = $false
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 2
            try {
                $resp = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3
                $ready = $true
                break
            } catch {}
        }
        if ($ready) {
            Write-OK "Ollama service is ready"
        } else {
            Write-Fail "Ollama did not respond after 30s. Check system tray or rerun."
            exit 1
        }
    } else {
        Write-Fail "ollama.exe not found at $ollamaExe"
        Write-Host "    Run .\scripts\setup.ps1 to install Ollama."
        exit 1
    }
} else {
    Write-OK "Ollama is running (PID $($proc.Id))"
}

# ---------------------------------------------------------------------------
# 2. Confirm API is responding
# ---------------------------------------------------------------------------
Write-Step "Verifying Ollama API"
try {
    $tags = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 5
    Write-OK "Ollama API is responding at http://localhost:11434"
} catch {
    Write-Fail "Ollama API did not respond: $_"
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Show available models
# ---------------------------------------------------------------------------
Write-Step "Available models"
ollama list

# ---------------------------------------------------------------------------
# 4. Check required profiles exist
# ---------------------------------------------------------------------------
Write-Step "Checking agent profiles"
$models = ollama list 2>&1
$has32k = $models -match "qwen36-agent-32k"
$has64k = $models -match "qwen36-agent-64k"

if ($has32k) {
    Write-OK "qwen36-agent-32k profile found"
} else {
    Write-Warn "qwen36-agent-32k profile NOT found. Run .\scripts\setup.ps1 to create it."
}

if ($has64k) {
    Write-OK "qwen36-agent-64k profile found"
} else {
    Write-Warn "qwen36-agent-64k profile NOT found. Run .\scripts\setup.ps1 to create it."
}

# ---------------------------------------------------------------------------
# 5. GPU status
# ---------------------------------------------------------------------------
Write-Step "GPU status"
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader

# ---------------------------------------------------------------------------
# 6. Currently loaded models
# ---------------------------------------------------------------------------
Write-Step "Currently loaded models (in VRAM)"
ollama ps

Write-Host "`nOllama is ready. Open VS Code, select Roo Code, set model to qwen36-agent-32k." -ForegroundColor Green
