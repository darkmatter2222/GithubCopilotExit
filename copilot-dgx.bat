@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM GitHub Copilot CLI DGX Spark launcher
REM Maximum-autonomy local model launcher
REM Voice input is enabled inside the session with /voice
REM IMPORTANT: Do NOT name this file copilot.bat
REM Recommended name: copilot-dgx.bat
REM ============================================================

set COPILOT_PROVIDER_TYPE=openai
set COPILOT_PROVIDER_BASE_URL=http://192.168.86.39:8001/v1
set COPILOT_PROVIDER_API_KEY=no-key
set COPILOT_ALLOW_ALL=true

REM Find the real Copilot CLI, not this launcher.
set REAL_COPILOT=

for /f "delims=" %%I in ('where copilot 2^>nul') do (
    if /I not "%%~fI"=="%~f0" (
        set REAL_COPILOT=%%~fI
        goto found_copilot
    )
)

:found_copilot
if "%REAL_COPILOT%"=="" (
    echo ERROR: Could not find the real GitHub Copilot CLI on PATH.
    echo.
    echo Try running:
    echo   where copilot
    echo.
    echo If the first result is this .bat file, rename this file to copilot-dgx.bat.
    exit /b 1
)

echo.
echo Select GitHub Copilot CLI model:
echo.
echo   1. Qwen3.6-27B
echo   2. Qwen3-Coder 27B
echo   3. Qwen3.6-27B OBLITERATED
echo   4. Exit
echo.

choice /C 1234 /N /M "Choose a model [1-4]: "

if errorlevel 4 goto exit
if errorlevel 3 goto obliterated
if errorlevel 2 goto qwen3coder
if errorlevel 1 goto qwen3

:qwen3
set COPILOT_MODEL=qwen3
set MODEL_DISPLAY_NAME=Qwen3.6-27B
goto launch

:qwen3coder
set COPILOT_MODEL=qwen3-coder
set MODEL_DISPLAY_NAME=Qwen3-Coder 27B
goto launch

:obliterated
set COPILOT_MODEL=obliterated
set MODEL_DISPLAY_NAME=Qwen3.6-27B OBLITERATED
goto launch

:launch
echo.
echo Starting GitHub Copilot CLI
echo ------------------------------------------------------------
echo   Real CLI:              %REAL_COPILOT%
echo   Provider type:         %COPILOT_PROVIDER_TYPE%
echo   Base URL:              %COPILOT_PROVIDER_BASE_URL%
echo   Model ID:              %COPILOT_MODEL%
echo   Model name:            %MODEL_DISPLAY_NAME%
echo   Mode:                  Autopilot / agent-like
echo   Permissions:           YOLO / allow-all / never ask
echo   Remote mobile:         ON
echo   Streaming:             ON
echo   Context:               Long context
echo   GitHub MCP tools:      ALL
echo   Experimental:          ON
echo   Autopilot continues:   Unlimited
echo   Voice input:           Use /voice inside Copilot CLI
echo ------------------------------------------------------------
echo.
echo In-session setup commands:
echo   /voice        Enable speech-to-text dictation
echo   /remote on    Ensure remote mobile control is enabled
echo   /env          Show loaded tools, MCP servers, skills, hooks
echo   /mcp show     Show configured MCP servers
echo   /yolo         Re-enable allow-all permissions
echo.
echo Voice usage after /voice is enabled:
echo   Hold Spacebar to talk, release to insert transcription.
echo   For longer dictation: Ctrl+X, then V.
echo.
echo WARNING:
echo   This can read, modify, delete, and execute files under this repo.
echo   Use only in a repo/folder you trust.
echo.

call "%REAL_COPILOT%" ^
  --mode=autopilot ^
  --yolo ^
  --no-ask-user ^
  --remote ^
  --stream=on ^
  --context=long_context ^
  --enable-all-github-mcp-tools ^
  --experimental

goto end

:exit
echo Exiting.
goto end

:end
endlocal