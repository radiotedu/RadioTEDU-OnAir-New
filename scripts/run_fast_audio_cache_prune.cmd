@echo off
setlocal
C:\Windows\py.exe -3 "%~dp0prune_fast_audio_cache.py" --min-age-seconds 900 --max-deletions 2000
exit /b %ERRORLEVEL%
