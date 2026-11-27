@echo off
setlocal enabledelayedexpansion
title TTS (IN NET CREATIONS) — Git Push Helper
color 0B
cls

cd /d "%~dp0"

echo.
echo  ============================================================
echo   TTS (IN NET CREATIONS) — Git Push
echo   Powered by IN NET CREATIONS ^| Created by Manohar.S
echo  ============================================================
echo.

where git >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Git is not installed or not in PATH.
    pause
    exit /b 1
)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
echo Current branch: %BRANCH%
echo.
git status -s
echo.

set "COMMIT_MSG="
set /p "COMMIT_MSG=Enter commit message (press Enter for default): "
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=Update TTS (IN NET CREATIONS)"

echo.
echo [1/3] Staging all changes...
git add -A
if %errorlevel% neq 0 (
    echo [ERROR] git add failed.
    pause
    exit /b 1
)

echo [2/3] Committing changes...
git diff --cached --quiet
if !errorlevel! equ 0 (
    echo        No new changes to commit. Proceeding to push...
) else (
    git commit -m "!COMMIT_MSG!"
    if !errorlevel! neq 0 (
        echo [ERROR] git commit failed.
        pause
        exit /b 1
    )
)

echo [3/3] Pushing to origin/%BRANCH%...
git push -u origin "%BRANCH%"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] git push failed. Check your GitHub authentication and remote settings.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   SUCCESS! Pushed to origin/%BRANCH%
echo  ============================================================
echo.
pause

