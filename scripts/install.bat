@echo off
title Direto XR Controller - Setup

:: Always run from project root (parent of scripts\)
cd /d "%~dp0.."

echo.
echo  =============================================
echo   Direto XR Controller - Windows Installer
echo  =============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo  Install from https://www.python.org/downloads/
    echo  Tick "Add Python to PATH" during installation.
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  [OK] Found Python %PYVER%

:: Delete old venv if it exists so we always get a clean one for this Python
if exist "venv\" (
    echo  [INFO] Removing old virtual environment...
    rmdir /s /q venv
)

:: Create venv
echo  [SETUP] Creating virtual environment...
python -m venv venv
if errorlevel 1 (
    echo  [ERROR] Failed to create virtual environment.
    pause & exit /b 1
)
echo  [OK] Virtual environment created.

:: Activate
call venv\Scripts\activate.bat

:: Upgrade pip
echo  [SETUP] Upgrading pip...
python -m pip install --upgrade pip -q

:: Install deps - run without --quiet so errors are visible
echo  [SETUP] Installing dependencies (may take a minute)...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  [ERROR] Dependency installation failed. See errors above.
    pause & exit /b 1
)

echo.
echo  [OK] All dependencies installed.
echo.
echo  =============================================
echo   Setup complete! Run:  scripts\start.bat
echo  =============================================
echo.
pause
