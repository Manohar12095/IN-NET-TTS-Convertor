@echo off
title TTS (IN NET CREATIONS) — Launcher
color 0B
cls

echo.
echo  ============================================================
echo   TTS (IN NET CREATIONS)
echo   Powered by IN NET CREATIONS
echo   Created by Manohar.S  ^|  innetcreations@gmail.com
echo  ============================================================
echo.

:: ── Check Python ──────────────────────────────────────────
echo [1/3] Checking Python installation...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.8+ from https://python.org
    echo         Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
echo        Python found. OK

:: ── Install / upgrade dependencies ────────────────────────
echo [2/3] Checking dependencies (flask, edge-tts>=7.3.1)...
python -c "import flask, edge_tts; v=tuple(int(x) for x in edge_tts.__version__.split('.')[:3]); assert v >= (7, 3, 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo        Installing / upgrading dependencies...
    python -m pip install -r "%~dp0requirements.txt" --upgrade --quiet --no-warn-script-location
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies. Check your internet connection and try again.
        pause
        exit /b 1
    )
)
echo        Dependencies OK

:: ── Launch Flask server ────────────────────────────────────
echo [3/3] Starting TTS (IN NET CREATIONS)...
echo.
echo  ============================================================
echo   Access at: http://127.0.0.1:5000
echo   Press Ctrl+C to stop the server
echo  ============================================================
echo.

:: Change to the script's own directory so Python can find app.py
cd /d "%~dp0"
python app.py

:: ── Pause on exit so user can see any error messages ──────
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The server stopped unexpectedly. See the message above.
    pause
)
