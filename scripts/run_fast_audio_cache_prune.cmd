@echo off
setlocal
C:\Windows\py.exe -3 "%~dp0prune_fast_audio_cache.py" --max-bytes 4294967296 --min-age-seconds 900 --max-deletions 2000
exit /b %ERRORLEVEL%
