@echo off
REM Double-click to open a terminal with the agent running (REPL).
REM Also works with a task or flags:  start.cmd "your task"   /   start.cmd --serve
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title micro-agent

REM Find a uv launcher: prefer `uv` on PATH, else `python -m uv`.
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 (
    python -m uv --version >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   First-time setup needed. Run the installer once:
        echo.
        echo       powershell -ExecutionPolicy Bypass -File scripts\install.ps1
        echo.
        echo   ^(installs uv + dependencies^), then double-click this file again.
        echo.
        pause
        exit /b 1
    )
    set "UV=python -m uv"
)

REM `uv run` auto-creates .venv and installs deps on first run.
REM No args -> interactive start menu; with args -> pass straight through.
if "%~1"=="" (
    %UV% run agent --menu
) else (
    %UV% run agent %*
)

echo.
echo [agent exited] press any key to close...
pause >nul
