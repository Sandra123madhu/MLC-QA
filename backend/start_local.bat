@echo off
setlocal

echo.
echo ==================================================
echo   MLC QA Local Server Runner
echo ==================================================
echo.

:: 1. Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in your system PATH.
    echo.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: 2. Check for Virtual Environment
if not exist "venv\" (
    echo [INFO] Virtual environment not found. Running setup...
    python local_setup.py
    if %errorlevel% neq 0 (
        echo [ERROR] Setup failed. Please check the errors above.
        pause
        exit /b 1
    )
)

:: 3. Start Server
echo [INFO] Starting the backend server...
cd /d "%~dp0"
.\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 10000 --reload

if %errorlevel% neq 0 (
    echo [ERROR] Failed to start server. Ensure you installed dependencies!
    pause
)
pause
