@echo off
setlocal

title Direto XR Controller

:: ── Always run from project root ──────────────────────────────────────────────
cd /d "%~dp0.."

:: ── Check venv ────────────────────────────────────────────────────────────────
if not exist "venv\Scripts\activate.bat" (
    echo  [ERROR] Virtual environment not found.
    echo  Please run scripts\install.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo.
echo  =============================================
echo   Direto XR Controller
echo  =============================================
echo.
echo  Server starting at http://localhost:8000
echo  Press Ctrl+C to stop.
echo.

start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"

python direto_server.py

echo.
echo  Server stopped.
pause
