@echo off
setlocal EnableExtensions
set "BUNDLE_ROOT=%~dp0"
set "APP_ROOT=%BUNDLE_ROOT%app"
set "SUPPORT_ROOT=%LOCALAPPDATA%\RadioTEDU\OnAir-Portable"
set "DATA_ROOT=%SUPPORT_ROOT%\data"
set "USER_ROOT=%SUPPORT_ROOT%\user"
set "VENV_ROOT=%SUPPORT_ROOT%\venv"

where py >nul 2>&1 || (echo Python 3 is required.& pause & exit /b 1)
if not exist "%VENV_ROOT%\Scripts\python.exe" py -3 -m venv "%VENV_ROOT%"
"%VENV_ROOT%\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%APP_ROOT%\requirements.txt" || exit /b 1

set "MEDIA_ROOT=H:\"
if not exist "%MEDIA_ROOT%" set /p "MEDIA_ROOT=Enter the RadioTEDU media root: "
if not exist "%MEDIA_ROOT%" (echo Media root not found.& pause & exit /b 1)

if not exist "%SUPPORT_ROOT%\portable-import.done" (
  set "RADIOTEDU_BACKUP_PASSWORD=radiotedu"
  "%VENV_ROOT%\Scripts\python.exe" "%APP_ROOT%\tools\import_portable_recovery.py" --bundle-root "%BUNDLE_ROOT%" --data-root "%DATA_ROOT%" --user-config-root "%USER_ROOT%" --source-drive "H:" --media-root "%MEDIA_ROOT%" || exit /b 1
  type nul > "%SUPPORT_ROOT%\portable-import.done"
)

set "CLEANROOM_PORT=18110"
set "CLEANROOM_DB_PATH=%DATA_ROOT%\cleanroom.db"
set "CLEANROOM_DATA_ROOT=%DATA_ROOT%"
set "CLEANROOM_USER_CONFIG_ROOT=%USER_ROOT%"
set "CLEANROOM_CREDENTIAL_STORE_FILE=%USER_ROOT%\secrets\station-credentials.json"
set "CLEANROOM_OPEN_PANEL=0"
set "CLEANROOM_SKIP_STARTUP_AI=1"
set "CLEANROOM_DISABLE_LOCAL_PLAYBACK=1"
set "RADIOTEDU_PROCESS_ISOLATED_WORKERS=1"
set "RADIOTEDU_MEDIA_ROOT=%MEDIA_ROOT%"
set "RADIOTEDU_FFMPEG_PATH=%BUNDLE_ROOT%private\windows-tools\bin\ffmpeg.exe"
set "RADIOTEDU_FFPROBE_PATH=%BUNDLE_ROOT%private\windows-tools\bin\ffprobe.exe"

if not exist "%RADIOTEDU_FFMPEG_PATH%" (echo Private FFmpeg tools are missing.& pause & exit /b 1)
start "RadioTEDU OnAir" /B "%VENV_ROOT%\Scripts\python.exe" "%APP_ROOT%\run_cleanroom.py"
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:18110/?station_id=1#onair"
endlocal
