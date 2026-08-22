@echo off
setlocal
C:\Windows\py.exe -3 "%~dp0export_play_history.py" --db-path C:\ProgramData\RadioTEDU\OnAir\cleanroom.db
exit /b %ERRORLEVEL%

