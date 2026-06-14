# start-proxy-local.ps1 — Run the LLM proxy locally against the remote Ollama server.
# Usage: .\scripts\start-proxy-local.ps1
# The proxy listens on http://localhost:8001 and forwards to Ollama on the GPU server.
# Reads SSH_HOST from .env to build the Ollama URL (http://<SSH_HOST>:11434).

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force

# Load .env if present
$envFile = Join-Path $PSScriptRoot "..\\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), 'Process')
        }
    }
}

$env:OLLAMA_BASE_URL   = if ($env:SSH_HOST) { "http://$($env:SSH_HOST):11434" } else { "http://localhost:11434" }
$env:SERVED_MODEL_NAME = "qwen3"
$env:MIN_TEMPERATURE   = "0.6"
$env:API_PORT          = if ($env:API_PORT) { $env:API_PORT } else { "8001" }

Write-Host "Starting LLM proxy on http://localhost:$env:API_PORT"
Write-Host "  Ollama:   $env:OLLAMA_BASE_URL"
Write-Host "  Model:    $env:SERVED_MODEL_NAME"
Write-Host "  Min temp: $env:MIN_TEMPERATURE"
Write-Host ""

# Use the .venv uvicorn directly — avoids PATH issues when venv isn't activated
$uvicorn = Join-Path $PSScriptRoot "..\\.venv\\Scripts\\uvicorn.exe"
if (-not (Test-Path $uvicorn)) {
    Write-Error ".venv not found. Run: pip install -r proxy/requirements.txt --target .venv"
    exit 1
}

Push-Location "$PSScriptRoot\\..\\proxy"
& $uvicorn main:app --host 0.0.0.0 --port $env:API_PORT --log-level info
Pop-Location
