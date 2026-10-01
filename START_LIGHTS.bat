@echo off
rem Starts the room-lights controller (real lights). Add --simulate to run against the built-in simulator.
setlocal
cd /d "%~dp0"
title Room Lights controller

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment, this takes a minute...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python 3.11 or newer is required. Install it from python.org and run this again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)

rem Make sure the packages are all there (cheap when they are).
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, numpy, qrcode, tinytuya" 2>nul || (
  echo Installing missing packages...
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)

echo %* | findstr /i "simulate" >nul
if errorlevel 1 (
  if not exist "config\tuya_devices.json" (
    echo.
    echo config\tuya_devices.json is missing - copy your local secret config into the config folder first.
    echo ^(It is created by tools\tuya_login.py and is never stored in Git.^)
    pause
    exit /b 1
  )
  if not exist "config\tuya_fixtures.json" (
    echo.
    echo config\tuya_fixtures.json is missing - copy your local secret config into the config folder first.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" run.py %*
echo.
echo The controller has stopped. The room was restored.
timeout /t 5 >nul
