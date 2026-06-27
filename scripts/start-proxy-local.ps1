# start-proxy-local.ps1 - Start the LLM proxy locally (RTX 5090 fallback).
# Reads config from .env at repo root.
#
# Usage: .\scripts\start-proxy-local.ps1
# Prerequisites: .\scripts\setup-local.ps1 run once to create .venv

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force

$repo = Join-Path $PSScriptRoot ".."

# Load .env
$envFile = Join-Path $repo ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match "^\s*[^#]\S*=\S" } | ForEach-Object {
        $parts = $_ -split "=", 2
        if ($parts.Count -eq 2) {
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
        }
    }
    Write-Host "    .env loaded" -ForegroundColor Gray
}

# Defaults
if (-not $env:OLLAMA_BASE_URL)        { $env:OLLAMA_BASE_URL        = "http://localhost:11434" }
if (-not $env:MIN_TEMPERATURE)        { $env:MIN_TEMPERATURE        = "0.6" }
if (-not $env:DISABLE_THINKING_FOR_TOOLS) { $env:DISABLE_THINKING_FOR_TOOLS = "true" }
if (-not $env:ROUTER_REFRESH_S)       { $env:ROUTER_REFRESH_S       = "30" }
if (-not $env:API_PORT)               { $env:API_PORT               = "8001" }

Write-Host ""
Write-Host "==> Starting LLM Proxy (local)" -ForegroundColor Cyan
Write-Host "    Port    : http://localhost:$env:API_PORT"
Write-Host "    Ollama  : $env:OLLAMA_BASE_URL"
if ($env:VLLM_BASE_URL) { Write-Host "    vLLM    : $env:VLLM_BASE_URL" }
if ($env:MONGO_URI)     { Write-Host "    MongoDB : enabled" }
Write-Host ""

$uvicorn = Join-Path $repo ".venv\Scripts\uvicorn.exe"
if (-not (Test-Path $uvicorn)) {
    Write-Error ".venv not found. Run: .\scripts\setup-local.ps1 first"
    exit 1
}

Push-Location (Join-Path $repo "proxy")
& $uvicorn main:app --host 0.0.0.0 --port $env:API_PORT --log-level info
Pop-Location
