@echo off
REM Windows Troubleshooting Helper Script

setlocal enabledelayedexpansion

echo ========================================
echo Noise Analysis Platform - Troubleshooting
echo ========================================
echo.

:menu
echo What would you like to do?
echo 1. Fresh Installation
echo 2. Check Python Version
echo 3. List Python Packages
echo 4. Reinstall Dependencies
echo 5. Clear Cache
echo 6. Start Server
echo 7. Exit
echo.

set /p choice=Enter choice (1-7): 

if "%choice%"=="1" goto fresh_install
if "%choice%"=="2" goto check_python
if "%choice%"=="3" goto list_packages
if "%choice%"=="4" goto reinstall_deps
if "%choice%"=="5" goto clear_cache
if "%choice%"=="6" goto start_server
if "%choice%"=="7" goto exit
goto menu

:fresh_install
echo.
echo Starting fresh installation...
cd backend
if exist venv rmdir /s /q venv
python -m venv venv
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
mkdir ..\uploads 2>nul
mkdir ..\logs 2>nul
echo Installation complete!
pause
goto menu

:check_python
echo.
python --version
where python
pause
goto menu

:list_packages
echo.
cd backend
call venv\Scripts\activate.bat
pip list
pause
goto menu

:reinstall_deps
echo.
cd backend
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
echo Reinstall complete!
pause
goto menu

:clear_cache
echo.
echo Clearing cache...
cd backend
if exist __pycache__ rmdir /s /q __pycache__
if exist .pytest_cache rmdir /s /q .pytest_cache
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo Cache cleared!
pause
goto menu

:start_server
echo.
cd backend
call venv\Scripts\activate.bat
echo Starting server at http://localhost:5001
python app.py
pause
goto menu

:exit
echo Goodbye!
exit /b
