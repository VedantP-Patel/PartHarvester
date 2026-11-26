@echo off
title PartHarvester Launcher
cd /d "%~dp0"

echo ==========================================================
echo   PartHarvester - Multi-Part Batch Downloader Launcher
echo ==========================================================
echo.

set "PY_EXE=%LocalAppData%\Python\pythoncore-3.15-64\python.exe"

if exist "%PY_EXE%" (
    goto RUN
)

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_EXE=py"
    goto RUN
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_EXE=python"
    goto RUN
)

echo [ERROR] Python was not found. Please install Python or ensure it is in PATH.
pause
exit /b 1

:RUN
"%PY_EXE%" -c "import fastapi, uvicorn, curl_cffi" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Installing Python dependencies from requirements.txt...
    "%PY_EXE%" -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)
echo Starting PartHarvester Web Edition...
"%PY_EXE%" main.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server terminated with an error.
    pause
)

