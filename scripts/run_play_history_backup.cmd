@echo off
setlocal
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup_play_history_to_github.ps1"
exit /b %ERRORLEVEL%

