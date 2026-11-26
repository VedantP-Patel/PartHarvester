@echo off
setlocal enabledelayedexpansion
title PartHarvester v2 Launcher
cd /d "%~dp0"

set "BIN=target\release\partharvester-tauri.exe"
if exist "%BIN%" goto LAUNCH

echo ==========================================================
echo   PartHarvester v2 - First-Time Release Build (Windows)
echo ==========================================================
echo.

if exist "D:\Projects\.toolchains\cargo\bin" (
    set "CARGO_HOME=D:\Projects\.toolchains\cargo"
    set "RUSTUP_HOME=D:\Projects\.toolchains\rustup"
    set "PATH=D:\Projects\.toolchains\cargo\bin;%PATH%"
) else if exist "%USERPROFILE%\.cargo\bin" (
    set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)

if exist "C:\msys64\ucrt64\bin\gcc.exe" (
    set "PATH=C:\msys64\ucrt64\bin;%PATH%"
    set "CC_x86_64_pc_windows_gnu=C:/msys64/ucrt64/bin/gcc.exe"
    set "CXX_x86_64_pc_windows_gnu=C:/msys64/ucrt64/bin/g++.exe"
    set "CC=C:/msys64/ucrt64/bin/gcc.exe"
    set "CXX=C:/msys64/ucrt64/bin/g++.exe"
    set "BINDGEN_EXTRA_CLANG_ARGS=--target=x86_64-pc-windows-gnu -isystem C:/msys64/ucrt64/include"
)
if exist "C:\Program Files\CMake\bin" set "PATH=C:\Program Files\CMake\bin;%PATH%"
if exist "C:\Program Files\LLVM\bin" (
    set "LIBCLANG_PATH=C:\Program Files\LLVM\bin"
    set "PATH=C:\Program Files\LLVM\bin;%PATH%"
)
if exist "C:\Program Files\NASM" set "PATH=C:\Program Files\NASM;%PATH%"

set "MISSING="
where node >nul 2>&1 || set "MISSING=!MISSING! Node.js"
where cargo >nul 2>&1 || set "MISSING=!MISSING! Rust/Cargo"
where cmake >nul 2>&1 || set "MISSING=!MISSING! CMake"
where nasm >nul 2>&1 || set "MISSING=!MISSING! NASM"

if not "%MISSING%"=="" (
    echo [ERROR] Missing build dependencies:%MISSING%
    echo.
    echo If you are a NORMAL USER (no coding tools needed):
    echo   1. Download the pre-built .exe from GitHub Releases, OR
    echo   2. Use the Python Web Version by running:  ..\run.bat
    echo.
    echo If you are a DEVELOPER building from source:
    echo   winget install OpenJS.NodeJS.LTS Rustlang.Rustup Kitware.CMake LLVM.LLVM NASM.NASM
    echo.
    pause
    exit /b 1
)

if not exist "ui\dist\index.html" (
    echo Building Svelte 5 UI...
    call npm --prefix ui install
    call npm --prefix ui run build
    if errorlevel 1 (
        echo [ERROR] Frontend build failed.
        pause
        exit /b 1
    )
)

echo Building native desktop executable...
cargo build --release -p partharvester-tauri --features custom-protocol
if errorlevel 1 (
    echo.
    echo [ERROR] Native build failed.
    pause
    exit /b 1
)

:LAUNCH
start "" "%BIN%"
