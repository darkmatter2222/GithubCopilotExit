# start-proxy-local.ps1 — Start the LLM proxy on this machine.
# The proxy listens on http://localhost:8001 and forwards to local Ollama at localhost:11434.
# Run this script every time you start a session before using VS Code Copilot chat.
#
# Prerequisites:
#   1. Ollama installed (https://ollama.com) with the model alias created (run setup-local.ps1 once)
#   2. pip install -r proxy/requirements.txt (run once after cloning)
#   3. Model configured in .env (OLLAMA_MODEL + SERVED_MODEL_NAME)
#
# Usage:  .\scripts\start-proxy-local.ps1

Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force

# ── Load .env into environment variables ───────────────────────────────────
$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match '^\s*[^#]\S+=\S' } | ForEach-Object {
        $parts = $_ -split '=', 2
        if ($parts.Count -eq 2) {
            $k = $parts[0].Trim()
            $v = $parts[1].Trim()
            [System.Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
    Write-Host "    .env loaded" -ForegroundColor Gray
}

# Apply defaults for any values not set in .env
if (-not $env:OLLAMA_BASE_URL)   { $env:OLLAMA_BASE_URL   = "http://localhost:11434" }
if (-not $env:SERVED_MODEL_NAME) { $env:SERVED_MODEL_NAME = "gemma-coder" }
if (-not $env:MIN_TEMPERATURE)   { $env:MIN_TEMPERATURE   = "0.6" }
if (-not $env:API_PORT)          { $env:API_PORT           = "8001" }

Write-Host "==> Starting LLM proxy" -ForegroundColor Cyan
Write-Host "    Listening : http://localhost:$env:API_PORT"
Write-Host "    Ollama    : $env:OLLAMA_BASE_URL"
Write-Host "    Model     : $env:SERVED_MODEL_NAME"
Write-Host "    Min temp  : $env:MIN_TEMPERATURE"
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
