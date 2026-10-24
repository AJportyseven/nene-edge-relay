@echo off
REM ============================================================
REM  NENE Edge TTS relay - Windows one-click start
REM  Requirement: Python 3.10+ installed and on PATH ("py" launcher).
REM  Runs on port 8123 by default. Override with:  --port 9000
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating Python virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create venv. Install Python from python.org and retry.
        exit /b 1
    )
)

echo [2/3] Installing dependencies (edge-tts, miniaudio)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed. Check your internet connection.
    exit /b 1
)

echo [3/3] Starting relay...
echo.
if not defined GROQ_API_KEY (
  echo WARNING: GROQ_API_KEY not set in environment. Add to system env or set here.
)
".venv\Scripts\python.exe" edge_relay.py %*
pause