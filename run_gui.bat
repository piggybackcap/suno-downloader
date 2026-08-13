@echo off
setlocal EnableExtensions

rem Always run from the script's directory, no matter where it is called from
cd /d "%~dp0"

set "UV_EXE="

where uv >nul 2>&1
if not errorlevel 1 (
    set "UV_EXE=uv"
    goto :uv_found
)

if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
    goto :uv_found
)

if exist "%USERPROFILE%\.cargo\bin\uv.exe" (
    set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
    goto :uv_found
)

echo Installing uv...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo Failed to install uv.
    exit /b 1
)

if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
    goto :uv_found
)

if exist "%USERPROFILE%\.cargo\bin\uv.exe" (
    set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
    goto :uv_found
)

echo uv was not found after installation.
exit /b 1

:uv_found
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    "%UV_EXE%" sync
    if errorlevel 1 (
        echo uv sync failed.
        exit /b 1
    )
)

"%UV_EXE%" run suno_download_gui.py
exit /b %ERRORLEVEL%
