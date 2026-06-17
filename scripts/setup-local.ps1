# setup-local.ps1 — SETUP / MODEL SWAP. Re-run whenever you change the model in .env.
#
# To swap models:
#   1. Edit .env — set OLLAMA_MODEL and SERVED_MODEL_NAME to the new values
#   2. Run this script — it pulls the model, creates the alias, removes the old one
#   3. Restart the proxy:  .\scripts\start-proxy-local.ps1
#
# Model config lives in .env — never hardcode model names in scripts.
#
# Usage:  .\scripts\setup-local.ps1

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
$ErrorActionPreference = "Stop"

$repo = Join-Path $PSScriptRoot ".."
$venv = Join-Path $repo ".venv"

# ── Load .env ────────────────────────────────────────────────────────────────
$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) { Write-Error ".env not found at $envFile"; exit 1 }

$envVars = @{}
Get-Content $envFile | Where-Object { $_ -match '^\s*[^#]\S+=\S' } | ForEach-Object {
    $parts = $_ -split '=', 2
    if ($parts.Count -eq 2) { $envVars[$parts[0].Trim()] = $parts[1].Trim() }
}

$ollamaModel    = $envVars["OLLAMA_MODEL"]
$aliasName      = $envVars["SERVED_MODEL_NAME"]
$contextLen     = if ($envVars["OLLAMA_CONTEXT"]) { [int]$envVars["OLLAMA_CONTEXT"] } else { 262144 }

if (-not $ollamaModel) { Write-Error "OLLAMA_MODEL not set in .env"; exit 1 }
if (-not $aliasName)   { Write-Error "SERVED_MODEL_NAME not set in .env"; exit 1 }

Write-Host ""
Write-Host "==> Model config from .env:" -ForegroundColor Cyan
Write-Host "      OLLAMA_MODEL     : $ollamaModel"
Write-Host "      SERVED_MODEL_NAME: $aliasName"
Write-Host "      OLLAMA_CONTEXT   : $contextLen tokens"
Write-Host ""

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

# ── Step 3: Remove old models that are NO LONGER the active alias ─────────────
# Prevents two large models from sitting in Ollama simultaneously wasting disk.
Write-Host ""
Write-Host "==> Checking for stale model aliases to remove..." -ForegroundColor Cyan
$installed = @($tags.models | ForEach-Object { $_.name })
$staleAliases = @("gemma-coder", "gemma-coder:latest")  # add former aliases here when switching again
foreach ($stale in $staleAliases) {
    if ($stale -eq $aliasName -or "$stale`:latest" -eq "$aliasName`:latest") { continue }
    if ($installed -contains $stale -or $installed -contains "$stale`:latest") {
        Write-Host "    Removing stale alias: $stale" -ForegroundColor Yellow
        $null = ollama rm $stale 2>&1
    }
}
Write-Host "    Done." -ForegroundColor Green

# ── Step 4: Acquire the model weights ───────────────────────────────────────
#
# OLLAMA_MODEL supports two formats in .env:
#   hf.co/user/repo:filename   - HuggingFace GGUF (downloaded via huggingface_hub)
#   modelname:tag              - Ollama registry model (pulled via ollama pull)
#
# Note: HF repos using Xet storage cannot be pulled by Ollama directly.
# The hf.co/ path downloads via Python then creates the model from a local path.

Write-Host ""
$modelFrom = ""

if ($ollamaModel -like "hf.co/*") {
    # ── HuggingFace GGUF download ──────────────────────────────────────────
    $hfSpec = $ollamaModel.Substring(6)           # strip "hf.co/"
    $colIdx = $hfSpec.IndexOf(":")
    $hfRepo = $hfSpec.Substring(0, $colIdx)
    $hfFile = $hfSpec.Substring($colIdx + 1)
    if (-not $hfFile.EndsWith(".gguf")) { $hfFile += ".gguf" }

    Write-Host "==> Downloading GGUF from HuggingFace..." -ForegroundColor Cyan
    Write-Host "    Repo : $hfRepo" -ForegroundColor Gray
    Write-Host "    File : $hfFile" -ForegroundColor Gray
    Write-Host "    (progress shown below - this is a ~7 GB download)" -ForegroundColor Gray
    Write-Host ""

    # Inline Python - downloads to HF cache (~/.cache/huggingface/hub/...)
    $pyScript = @'
import sys, os
try:
    from huggingface_hub import hf_hub_download
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)
repo     = sys.argv[1]
filename = sys.argv[2]
path = hf_hub_download(repo_id=repo, filename=filename)
# Print ONLY the path to stdout so PS can capture it cleanly
print(path)
'@
    $tmp = New-TemporaryFile
    Set-Content $tmp.FullName $pyScript -Encoding UTF8

    Write-Host "    Downloading (stderr shows progress)..." -ForegroundColor Gray
    $localGguf = (& $python $tmp.FullName $hfRepo $hfFile) | Select-Object -Last 1
    $dlExit = $LASTEXITCODE
    Remove-Item $tmp.FullName -ErrorAction SilentlyContinue

    if ($dlExit -ne 0 -or -not $localGguf -or -not (Test-Path $localGguf)) {
        Write-Error "Download failed (exit $dlExit). Check output above."
        exit 1
    }
    Write-Host ""
    Write-Host "    Saved to: $localGguf" -ForegroundColor Green
    $modelFrom = $localGguf

} else {
    # ── Ollama registry pull ───────────────────────────────────────────────
    Write-Host "==> Pulling model from Ollama registry: $ollamaModel" -ForegroundColor Cyan
    ollama pull $ollamaModel
    if ($LASTEXITCODE -ne 0) { Write-Error "ollama pull failed"; exit 1 }
    $modelFrom = $ollamaModel
}
Write-Host "    Done." -ForegroundColor Green

# ── Step 5: Create named alias with context + sampling parameters ─────────────
Write-Host ""
Write-Host "==> Creating '$aliasName' alias (ctx=$contextLen)..." -ForegroundColor Cyan
$modelfile = "FROM $modelFrom`nPARAMETER num_ctx $contextLen`nPARAMETER temperature 1.0`nPARAMETER top_p 0.95`nPARAMETER top_k 64"
$tmp = New-TemporaryFile
Set-Content $tmp.FullName $modelfile -NoNewline
ollama create $aliasName -f $tmp.FullName
Remove-Item $tmp.FullName
Write-Host "    Done." -ForegroundColor Green

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==> Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "    Active model : $aliasName -> $ollamaModel"
Write-Host "    Context      : $contextLen tokens"
Write-Host ""
Write-Host "    To start the proxy:  .\scripts\start-proxy-local.ps1"
Write-Host "    Or all at once:      .\scripts\go.ps1"
Write-Host "    VS Code endpoint:    http://localhost:8001/v1/chat/completions"
Write-Host "    Live dashboard:      http://localhost:8001/dashboard"
Write-Host ""
