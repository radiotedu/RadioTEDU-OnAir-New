@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"
"%PYTHON%" scripts\package_rtai_onair.py --force
if errorlevel 1 goto :fail
start "" explorer.exe "%CD%\dist\editions"
exit /b 0

:fail
echo.
echo rtAI OnAir package build failed. No installed broadcaster was changed.
pause
exit /b 1
