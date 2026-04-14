@echo off
REM AI Orchestration System — Windows Start Script
REM Usage: start.bat [--once] [--dashboard]

cd /d "%~dp0"

IF NOT EXIST ".env" (
    echo ERROR: .env file not found.
    echo Copy .env.example to .env and fill in your API keys:
    echo   copy .env.example .env
    pause
    exit /b 1
)

python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: python not found. Please install Python 3.10+.
    pause
    exit /b 1
)

echo Checking dependencies...
python -c "import openai, anthropic, dotenv" >nul 2>&1
IF ERRORLEVEL 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
)

echo.
echo ================================================
echo   AI Orchestration System
echo ================================================
echo.

python runner.py %*
