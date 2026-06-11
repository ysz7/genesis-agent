@echo off
REM Double-click to open the wizard that scaffolds a NEW agent in a sibling folder.
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONUTF8=1
title genesis-agent - new agent

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
        pause
        exit /b 1
    )
    set "UV=python -m uv"
)

%UV% run agent --new

echo.
pause >nul
