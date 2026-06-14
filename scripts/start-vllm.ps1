<#
.SYNOPSIS
    Start Ollama serving Qwen3.6-27B with an OpenAI-compatible endpoint on port 8000.

.DESCRIPTION
    Starts Ollama (already installed) on the specified port with the qwen3.6:27b-mtp-q4_K_M
    model exposed as "qwen3". The endpoint is accessible at http://localhost:8000/v1.

    Compatible with any OpenAI-API client: Roo Code, Continue, Cursor, etc.
    Set model name to "qwen3" in your client.

    Features:
      - Native tool/function calling
      - 262k context window (256K tokens = 262,144)
      - Vision support (text + image input)
      - MTP (Multi-Token Prediction) for faster generation

.USAGE
    .\scripts\start-vllm.ps1

    Optional overrides:
    .\scripts\start-vllm.ps1 -OllamaModel "qwen3.6:27b-mtp-q4_K_M" -Port 8000

.NOTES
    Requirements:
      - Ollama installed at the default Windows path
      - The model already pulled via: ollama pull qwen3.6:27b-mtp-q4_K_M
    Model is loaded into GPU VRAM automatically by Ollama (~18GB VRAM for q4_K_M).
#>

param(
    [string]$OllamaModel = "qwen3.6:27b-mtp-q4_K_M",
    [string]$ServedName  = "qwen3",
    [int]   $Port        = 8000,
    [int]   $NumCtx      = 262144
)

$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"

if (-not (Test-Path $OllamaExe)) {
    Write-Error "Ollama not found at $OllamaExe. Install from https://ollama.com"
    exit 1
}

Write-Host "==> Starting Ollama on port $Port" -ForegroundColor Cyan
Write-Host "    Model  : $OllamaModel (served as '$ServedName')"
Write-Host "    Context: $NumCtx tokens (262k)"
Write-Host "    Tools  : enabled (native)"
Write-Host "    URL    : http://localhost:$Port/v1" -ForegroundColor Green
Write-Host ""

# Enable full 262k context window
$env:OLLAMA_NUM_CTX = $NumCtx
# Start Ollama server on the configured port
$env:OLLAMA_HOST = "127.0.0.1:$Port"
$serverProc = Start-Process -FilePath $OllamaExe -ArgumentList "serve" -PassThru -WindowStyle Hidden

Write-Host "==> Waiting for Ollama to be ready..." -NoNewline
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-RestMethod "http://localhost:$Port/api/tags" -ErrorAction Stop
        $ready = $true
        break
    } catch {}
    Write-Host "." -NoNewline
}
Write-Host ""

if (-not $ready) {
    Write-Error "Ollama did not start within 30 seconds"
    $serverProc | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

# Ensure the alias model exists with tool calling and full 262k context
$models = (Invoke-RestMethod "http://localhost:$Port/api/tags").models.name
if ($ServedName -notin $models -and "$ServedName`:latest" -notin $models) {
    Write-Host "==> Creating '$ServedName' alias for $OllamaModel (262k ctx, tool calling)..."
    $modelfile = @"
FROM $OllamaModel
PARAMETER num_ctx $NumCtx
"@
    $tmp = New-TemporaryFile
    $modelfile | Set-Content $tmp.FullName -NoNewline
    & $OllamaExe create $ServedName -f $tmp.FullName
    Remove-Item $tmp.FullName
}

Write-Host ""
Write-Host "==> Ollama is running on GPU. Endpoint ready:" -ForegroundColor Green
Write-Host "    http://localhost:$Port/v1"
Write-Host "    Model name   : $ServedName"
Write-Host "    Tool calling : enabled (native)"
Write-Host "    Context      : $NumCtx tokens"
Write-Host ""
Write-Host "Press Ctrl+C to stop."
try {
    $serverProc.WaitForExit()
} finally {
    $serverProc | Stop-Process -Force -ErrorAction SilentlyContinue
}
