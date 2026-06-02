<#
.SYNOPSIS
    GPU health and model residency check.
    Run during an AI agent session to monitor VRAM and model status.
#>

Write-Host "`n=== GPU Status ===" -ForegroundColor Cyan
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv

Write-Host "`n=== Ollama Loaded Models ===" -ForegroundColor Cyan
ollama ps

Write-Host "`n=== Live GPU Monitor (Ctrl+C to stop) ===" -ForegroundColor Cyan
Write-Host "Watching nvidia-smi every 2 seconds..."
nvidia-smi -l 2 --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv
