@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Radio TED U Classical

cd /d "%~dp0"

set "ROOT=%CD%"
set "APP_URL=http://127.0.0.1:8100/app?station_id=1"
set "MAIN_VENV=%ROOT%\.venv"
set "OMNI_VENV=%ROOT%\.venv-omnivoice"
set "MAIN_PY=%MAIN_VENV%\Scripts\python.exe"
set "OMNI_PY=%OMNI_VENV%\Scripts\python.exe"
set "HF_HOME=%ROOT%\.hf-cache"
set "CLEANROOM_OPEN_PANEL=1"
set "CLEANROOM_HOST=127.0.0.1"
set "CLEANROOM_PORT=8100"

echo ========================================
echo   Radio TED U Classical
echo ========================================
echo.
echo First run will:
echo   1. Install Python environments
echo   2. Install app + AI dependencies
echo   3. Download Qwen 0.5B and OmniVoice
echo   4. Seed Radio TED U Classical settings
echo   5. Start the dashboard
echo.

call :resolve_python
if errorlevel 1 goto :fail

if not exist "%MAIN_PY%" (
    echo [1/7] Creating main virtual environment...
    call %PYTHON_BOOTSTRAP% -m venv "%MAIN_VENV%"
    if errorlevel 1 goto :fail
)

echo [2/7] Installing main app dependencies...
call "%MAIN_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail
call "%MAIN_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
call "%MAIN_PY%" -m pip install -r requirements-ai.txt
if errorlevel 1 goto :fail

if not exist "%OMNI_PY%" (
    echo [3/7] Creating OmniVoice virtual environment...
    call %PYTHON_BOOTSTRAP% -m venv "%OMNI_VENV%"
    if errorlevel 1 goto :fail
)

echo [4/7] Installing OmniVoice dependencies...
call "%OMNI_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail
call "%OMNI_PY%" -m pip install -r requirements-omnivoice.txt
if errorlevel 1 goto :fail

echo [5/7] Validating Python environments...
call "%MAIN_PY%" -c "import fastapi, uvicorn, transformers, edge_tts; print('main-env-ok')"
if errorlevel 1 goto :fail
call "%OMNI_PY%" -c "from omnivoice import OmniVoice; print('omnivoice-env-ok')"
if errorlevel 1 goto :fail

echo [6/7] Downloading models and seeding Radio TED U Classical...
call "%MAIN_PY%" scripts\bootstrap_radiotedu_classical.py
if errorlevel 1 goto :fail

echo [7/7] Starting dashboard...
echo Dashboard: %APP_URL%
echo Initial admin credentials: data\initial-admin-password.txt
echo.
call "%MAIN_PY%" run_cleanroom.py
goto :eof

:resolve_python
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_BOOTSTRAP=py -3.12"
        goto :eof
    )
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_BOOTSTRAP=py -3"
        goto :eof
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_BOOTSTRAP=python"
        goto :eof
    )
)

where winget >nul 2>nul
if not errorlevel 1 (
    echo Python 3.10+ not found. Installing Python 3.12 with winget...
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto :python_missing
    goto :resolve_python
)

:python_missing
echo Python 3.10+ is required. Install Python 3.12 and run START.bat again.
exit /b 1

:fail
echo.
echo Setup failed. Review the output above.
pause
exit /b 1
