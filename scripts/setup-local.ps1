# setup-local.ps1 — ONE-TIME SETUP. Run this once after cloning the repo.
#
# What this does:
#   1. Installs Python dependencies into .venv
#   2. Verifies Ollama is installed and running
#   3. Pulls qwen3.6:27b-mtp-q4_K_M if not already downloaded (~18 GB)
#   4. Creates the 'qwen3' alias with 262K context baked in (required)
#
# After this runs once, just use:   .\scripts\start-proxy-local.ps1
#
# Usage:  .\scripts\setup-local.ps1

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force

$repo = Join-Path $PSScriptRoot ".."
$venv = Join-Path $repo ".venv"

# ── Step 1: Python deps ──────────────────────────────────────────────────────
Write-Host "==> Installing Python dependencies into .venv..." -ForegroundColor Cyan
if (-not (Test-Path $venv)) {
    python -m venv $venv
}
& "$venv\Scripts\pip.exe" install -r "$repo\proxy\requirements.txt" --quiet
Write-Host "    Done." -ForegroundColor Green

# ── Step 2: Verify Ollama ────────────────────────────────────────────────────
Write-Host ""
Write-Host "==> Checking Ollama..." -ForegroundColor Cyan
try {
    $tags = Invoke-RestMethod "http://localhost:11434/api/tags" -ErrorAction Stop
    Write-Host "    Ollama is running." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "    Ollama is NOT running or not installed." -ForegroundColor Red
    Write-Host "    Install from: https://ollama.com"
    Write-Host "    Then start Ollama and re-run this script."
    exit 1
}

# ── Step 3: Pull model ───────────────────────────────────────────────────────
$model = "qwen3.6:27b-mtp-q4_K_M"
$modelNames = $tags.models.name
if ($model -notin $modelNames -and "$model`:latest" -notin $modelNames) {
    Write-Host ""
    Write-Host "==> Pulling $model (~18 GB, this will take a while)..." -ForegroundColor Cyan
    ollama pull $model
} else {
    Write-Host "    $model already downloaded." -ForegroundColor Green
}

# ── Step 4: Create qwen3 alias with 262K context ────────────────────────────
Write-Host ""
Write-Host "==> Creating 'qwen3' alias with 262K context window..." -ForegroundColor Cyan
$modelfile = "FROM $model`nPARAMETER num_ctx 262144"
$tmp = New-TemporaryFile
Set-Content $tmp.FullName $modelfile -NoNewline
ollama create qwen3 -f $tmp.FullName
Remove-Item $tmp.FullName
Write-Host "    Done." -ForegroundColor Green

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==> Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "    To start the proxy:     .\scripts\start-proxy-local.ps1"
Write-Host "    VS Code endpoint:       http://localhost:8001/v1/chat/completions"
Write-Host "    Live dashboard:         http://localhost:8001/dashboard"
Write-Host ""
