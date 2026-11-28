@echo off
setlocal enabledelayedexpansion
title PartHarvester Web Edition Launcher
cd /d "%~dp0"

echo ==========================================================
echo   PartHarvester Web Edition - Portable Launcher (Windows)
echo ==========================================================
echo.

set "PY_CMD="
set "PY_ARG="

rem 1. Check standard Python Launcher (py -3) and PATH python (rejecting Microsoft Store stub)
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "PY_CMD=py"
        set "PY_ARG=-3"
        goto FOUND_PY
    )
)

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "PY_CMD=python"
        goto FOUND_PY
    )
)

rem 2. Fallback scan common Windows Python install directories (in case "Add Python to PATH" was unchecked)
for %%D in (
    "%LocalAppData%\Python\pythoncore-3.15-64"
    "%LocalAppData%\Programs\Python\Python313"
    "%LocalAppData%\Programs\Python\Python312"
    "%LocalAppData%\Programs\Python\Python311"
    "%LocalAppData%\Programs\Python\Python310"
    "C:\Python313"
    "C:\Python312"
    "C:\Python311"
    "C:\Program Files\Python313"
    "C:\Program Files\Python312"
    "C:\Program Files\Python311"
) do (
    if exist "%%~D\python.exe" (
        "%%~D\python.exe" -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            set "PY_CMD=%%~D\python.exe"
            goto FOUND_PY
        )
    )
)

echo [ERROR] Python 3.10+ was not found on your system.
echo.
echo Fix:
echo   1. Install Python 3.12+ via:  winget install Python.Python.3.12
echo      (or download from https://www.python.org/downloads/ and check "Add Python to PATH")
echo   2. Or download the pre-built Desktop .exe from GitHub Releases (no Python needed).
echo.
pause
exit /b 1

:FOUND_PY
set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo Creating isolated virtual environment ^(.venv^)...
    "%PY_CMD%" %PY_ARG% -m venv .venv
    if errorlevel 1 (
        echo [WARN] Could not create .venv; falling back to system Python.
        set "VENV_PY=%PY_CMD%"
        set "VENV_ARG=%PY_ARG%"
        goto CHECK_DEPS
    )
)
set "VENV_ARG="

:CHECK_DEPS
"%VENV_PY%" %VENV_ARG% -c "import fastapi, uvicorn, curl_cffi, cryptography, base58, psutil, pydantic, requests, aiofiles, websockets" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing Python dependencies into .venv...
    "%VENV_PY%" %VENV_ARG% -m pip install --upgrade pip >nul 2>&1
    "%VENV_PY%" %VENV_ARG% -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo.
        echo [ERROR] Failed to install Python dependencies.
        echo Check your internet connection or try running with Administrator privileges.
        pause
        exit /b 1
    )
)

set "HAS_EXTRACTOR=0"
if exist "C:\Program Files\7-Zip\7z.exe" set "HAS_EXTRACTOR=1"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "HAS_EXTRACTOR=1"
if exist "C:\Program Files\WinRAR\WinRAR.exe" set "HAS_EXTRACTOR=1"
if exist "C:\Program Files\WinRAR\UnRAR.exe" set "HAS_EXTRACTOR=1"
if exist "C:\Program Files (x86)\WinRAR\WinRAR.exe" set "HAS_EXTRACTOR=1"
where 7z >nul 2>&1 && set "HAS_EXTRACTOR=1"
where unrar >nul 2>&1 && set "HAS_EXTRACTOR=1"
if "%HAS_EXTRACTOR%"=="0" (
    echo [NOTE] Neither 7-Zip nor WinRAR was detected.
    echo        Downloads work normally, but Auto-Extract requires 7-Zip ^(winget install 7zip.7zip^) or WinRAR.
    echo.
)

echo Starting PartHarvester Web Edition...
"%VENV_PY%" %VENV_ARG% main.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server terminated with an error.
    pause
)
