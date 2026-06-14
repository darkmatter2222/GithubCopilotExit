# start-proxy-local.ps1 — Start the LLM proxy on this machine.
# The proxy listens on http://localhost:8001 and forwards to local Ollama at localhost:11434.
# Run this script every time you start a session before using VS Code Copilot chat.
#
# Prerequisites:
#   1. Ollama installed (https://ollama.com) with qwen3 alias created (run setup-local.ps1 once)
#   2. pip install -r proxy/requirements.txt (run once after cloning)
#
# Usage:  .\scripts\start-proxy-local.ps1

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force

$env:OLLAMA_BASE_URL   = "http://localhost:11434"
$env:SERVED_MODEL_NAME = "qwen3"
$env:MIN_TEMPERATURE   = "0.6"
$env:API_PORT          = "8001"

Write-Host "==> Starting LLM proxy" -ForegroundColor Cyan
Write-Host "    Listening : http://localhost:$env:API_PORT"
Write-Host "    Ollama    : $env:OLLAMA_BASE_URL"
Write-Host "    Model     : $env:SERVED_MODEL_NAME"
Write-Host "    Min temp  : $env:MIN_TEMPERATURE  (clamped for Qwen3 thinking mode)"
Write-Host ""

# Use .venv's uvicorn directly — avoids PATH/activation issues
$uvicorn = Join-Path $PSScriptRoot "..\\.venv\\Scripts\\uvicorn.exe"
if (-not (Test-Path $uvicorn)) {
    Write-Error ".venv not found. Run:  pip install -r proxy\requirements.txt -t .venv\Lib\site-packages"
    exit 1
}

Push-Location "$PSScriptRoot\\..\\proxy"
& $uvicorn main:app --host 0.0.0.0 --port $env:API_PORT --log-level info
Pop-Location
