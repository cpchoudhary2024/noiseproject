@echo off
REM Quick Start Script for Windows

echo 🚀 Noise Analysis Platform - Quick Start
echo ========================================
echo.

REM Check Python installation
echo ✓ Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python is not installed. Please install Python 3.8 or later.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo ✓ Found: %PYTHON_VERSION%
echo.

REM Create virtual environment
echo ✓ Creating virtual environment...
cd backend
python -m venv venv
echo ✓ Virtual environment created
echo.

REM Activate virtual environment
echo ✓ Activating virtual environment...
call venv\Scripts\activate.bat
echo ✓ Virtual environment activated
echo.

REM Install dependencies
echo ✓ Installing dependencies from requirements.txt...
python -m pip install --upgrade pip
pip install -r requirements.txt
echo ✓ All dependencies installed successfully
echo.

REM Create directories
echo ✓ Creating uploads directory...
mkdir ..\uploads 2>nul
mkdir ..\logs 2>nul
echo ✓ Directories created
echo.

REM Start the application
echo 🎉 Setup complete! Starting the application...
echo.
echo 📊 Noise Analysis Platform is running at:
echo 🌐 http://localhost:5001
echo.
echo Press Ctrl+C to stop the server
echo.

python app.py
pause
