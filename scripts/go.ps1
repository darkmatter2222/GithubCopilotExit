#Requires -Version 5.1
<#
.SYNOPSIS
    One command. Full cold start. Does everything.

.DESCRIPTION
    Run this from anywhere after a reboot (or any time) and it will:
      1. Start Ollama if it isn't running and wait for it to be ready
      2. Pull the model + create the qwen3 alias if missing
      3. Load the model into GPU VRAM (warmup) so first requests are instant
      4. Tear down any existing proxy on port 8001 and restart it fresh
      5. Wait until the stack is healthy and print the dashboard URL

    Idempotent - safe to re-run at any time. If something is already up it
    gets restarted cleanly, not duplicated.

.EXAMPLE
    # From inside the repo:
    .\scripts\go.ps1

    # From ANYWHERE (great for a desktop shortcut or shell profile alias):
    powershell -ExecutionPolicy Bypass -File "C:\Users\ryans\source\repos\GithubCopilotExit\scripts\go.ps1"
#>

param(
    [string]$RepoRoot = (Split-Path $PSScriptRoot -Parent)
)

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
$ErrorActionPreference = "Stop"

# ---- Helpers ----------------------------------------------------------------
function Step  ($msg) { Write-Host "`n  ==> $msg" -ForegroundColor Cyan }
function OK    ($msg) { Write-Host "      OK  $msg" -ForegroundColor Green }
function Warn  ($msg) { Write-Host "      !!  $msg" -ForegroundColor Yellow }
function Fail  ($msg) { Write-Host "      XX  $msg" -ForegroundColor Red; exit 1 }

function Wait-For {
    param([string]$Url, [string]$Label, [int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 600
        try {
            $null = Invoke-RestMethod $Url -TimeoutSec 3
            return $true
        } catch { }
    }
    Fail "$Label did not respond within ${TimeoutSec}s at $Url"
}

# ---- Paths ------------------------------------------------------------------
$venv       = Join-Path $RepoRoot ".venv"
$python     = Join-Path $venv "Scripts\python.exe"
$uvicorn    = Join-Path $venv "Scripts\uvicorn.exe"
$warmupPy   = Join-Path $RepoRoot "scripts\warmup.py"
$startProxy = Join-Path $RepoRoot "scripts\start-proxy-local.ps1"

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host "    Local AI Stack - Cold Start" -ForegroundColor Cyan
Write-Host "    Repo: $RepoRoot" -ForegroundColor Gray
Write-Host "  ================================================" -ForegroundColor Cyan

# ---- Sanity check: has setup been run? --------------------------------------
Step "Checking setup..."
if (-not (Test-Path $uvicorn)) {
    Fail ".venv not found. Run .\scripts\setup-local.ps1 first (one-time setup)."
}
OK ".venv found"

# ---- Step 1: Ollama ---------------------------------------------------------
Step "Checking Ollama (localhost:11434)..."

$ollamaUp = $false
try {
    $null = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3
    $ollamaUp = $true
} catch { }

if ($ollamaUp) {
    OK "Ollama is already running"
} else {
    Warn "Ollama is not running - starting it now..."

    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        Fail "Ollama not found in PATH. Install from https://ollama.com and re-run."
    }
    $ollamaExe = $ollamaCmd.Source

    # Start ollama serve minimized so it stays out of the way
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Minimized

    Write-Host "      Waiting for Ollama to be ready (up to 45s)..." -ForegroundColor Gray
    $null = Wait-For "http://localhost:11434/api/tags" "Ollama" 45
    OK "Ollama is now running"
}

# ---- Step 2: Verify/pull model ----------------------------------------------
Step "Checking model (qwen3 alias)..."

$tags = Invoke-RestMethod "http://localhost:11434/api/tags"
$installedNames = @($tags.models | ForEach-Object { $_.name })
$aliasPresent = ($installedNames -contains "qwen3:latest") -or ($installedNames -contains "qwen3")

if (-not $aliasPresent) {
    Warn "qwen3 alias not found. Running setup to pull model and create alias (~18 GB, one-time)..."
    & (Join-Path $RepoRoot "scripts\setup-local.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "setup-local.ps1 failed. Fix errors above and re-run." }
    OK "Model and alias ready"
} else {
    $foundNames = ($installedNames | Where-Object { $_ -like "qwen3*" }) -join ", "
    OK "qwen3 alias found: $foundNames"
}

# ---- Step 3: VRAM Warmup ----------------------------------------------------
Step "Loading model into GPU VRAM (warmup)..."
Write-Host "      This takes 15-30 seconds on first load. Subsequent requests will be instant." -ForegroundColor Gray

try {
    $warmupOut = & $python $warmupPy 2>&1
    $warmupStr = ($warmupOut | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Warn "Warmup returned non-zero exit. Output: $warmupStr"
    } else {
        OK "Model is live in VRAM"
        Write-Host "      $warmupStr" -ForegroundColor Gray
    }
} catch {
    Warn "Warmup exception: $($_.Exception.Message). Continuing - model will load on first VS Code request."
}

# ---- Step 4: Tear down existing proxy on port 8001 --------------------------
Step "Stopping any existing proxy on port 8001..."

$existing = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $ownerPids = @($existing | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerPid in $ownerPids) {
        try {
            $proc = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $ownerPid -Force
                OK "Killed PID $ownerPid ($($proc.ProcessName))"
            }
        } catch {
            Warn "Could not kill PID $ownerPid - $($_.Exception.Message)"
        }
    }
    Start-Sleep -Milliseconds 800
} else {
    OK "Port 8001 is free"
}

# ---- Step 5: Start proxy in a new window ------------------------------------
Step "Starting proxy (a new window will open for proxy logs)..."

if (-not (Test-Path $startProxy)) {
    Fail "start-proxy-local.ps1 not found at $startProxy"
}

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $startProxy
) -WindowStyle Normal

# ---- Step 6: Wait for health ------------------------------------------------
Step "Waiting for proxy to become healthy..."
Write-Host "      Polling http://localhost:8001/health ..." -ForegroundColor Gray

$healthy = $false
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 600
    try {
        $h = Invoke-RestMethod "http://localhost:8001/health" -TimeoutSec 3
        if ($h.status -eq "ok" -and $h.ollama -eq $true) {
            $healthy = $true
            break
        }
    } catch { }
}

if (-not $healthy) {
    Fail "Proxy did not become healthy within 25 seconds. Check the proxy window for errors."
}

# ---- Done -------------------------------------------------------------------
Write-Host ""
Write-Host "  ================================================" -ForegroundColor Green
Write-Host "    Stack is UP. Everything is ready." -ForegroundColor Green
Write-Host "  ------------------------------------------------" -ForegroundColor Green
Write-Host "    Proxy     : http://localhost:8001" -ForegroundColor White
Write-Host "    Health    : http://localhost:8001/health" -ForegroundColor White
Write-Host "    Dashboard : http://localhost:8001/dashboard" -ForegroundColor White
Write-Host "  ------------------------------------------------" -ForegroundColor Green
Write-Host "    VS Code: Ctrl+Shift+I  ->  Qwen3.6-27B (RTX 5090)" -ForegroundColor White
Write-Host "  ================================================" -ForegroundColor Green
Write-Host ""
