#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/app/run_cleanroom.py" ]]; then
  BUNDLE_ROOT="$SCRIPT_DIR"
  APP_ROOT="$SCRIPT_DIR/app"
else
  APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  BUNDLE_ROOT="$(cd "$APP_ROOT/.." && pwd)"
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This launcher is for macOS."
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew from the official installer..."
  /bin/bash -c "$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
fi

brew install python@3.12
PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"

has_fdk() {
  "$1" -hide_banner -encoders 2>/dev/null | /usr/bin/grep -q 'libfdk_aac'
}

FFMPEG_BIN=""
if command -v ffmpeg >/dev/null 2>&1 && has_fdk "$(command -v ffmpeg)"; then
  FFMPEG_BIN="$(command -v ffmpeg)"
else
  brew tap homebrew-ffmpeg/ffmpeg
  brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-fdk-aac --with-alt-name
  FORMULA_PREFIX="$(brew --prefix homebrew-ffmpeg/ffmpeg/ffmpeg)"
  if [[ -x "$FORMULA_PREFIX/bin/ffmpeg-alt" ]]; then
    FFMPEG_BIN="$FORMULA_PREFIX/bin/ffmpeg-alt"
  else
    FFMPEG_BIN="$FORMULA_PREFIX/bin/ffmpeg"
  fi
fi

if ! has_fdk "$FFMPEG_BIN"; then
  echo "The selected FFmpeg does not provide libfdk_aac; streams were not started."
  exit 1
fi
FFMPEG_NAME="${FFMPEG_BIN##*/}"
FFPROBE_NAME="${FFMPEG_NAME/ffmpeg/ffprobe}"
FFPROBE_BIN="${FFMPEG_BIN%/*}/$FFPROBE_NAME"
if [[ ! -x "$FFPROBE_BIN" ]]; then
  FFPROBE_BIN="$(command -v ffprobe || true)"
fi
if [[ -z "$FFPROBE_BIN" || ! -x "$FFPROBE_BIN" ]]; then
  echo "ffprobe was not found; streams were not started."
  exit 1
fi

SUPPORT_ROOT="$HOME/Library/Application Support/RadioTEDU/OnAir"
DATA_ROOT="$SUPPORT_ROOT/data"
USER_ROOT="$SUPPORT_ROOT/user"
VENV_ROOT="$SUPPORT_ROOT/venv"
"$PYTHON_BIN" -m venv "$VENV_ROOT"
"$VENV_ROOT/bin/python" -m pip install --disable-pip-version-check -r "$APP_ROOT/requirements.txt"

MEDIA_ROOT="${RADIOTEDU_MEDIA_ROOT:-}"
if [[ -z "$MEDIA_ROOT" && -d "/Volumes/RadioTEDU Media" ]]; then
  MEDIA_ROOT="/Volumes/RadioTEDU Media"
fi
if [[ -z "$MEDIA_ROOT" ]]; then
  echo "Enter or drag the mounted RadioTEDU media-disk root here, then press Return:"
  read -r MEDIA_ROOT
fi
MEDIA_ROOT="${MEDIA_ROOT%/}"
if [[ ! -d "$MEDIA_ROOT" ]]; then
  echo "Media root does not exist: $MEDIA_ROOT"
  exit 1
fi

IMPORT_MARKER="$SUPPORT_ROOT/portable-import.done"
if [[ ! -f "$IMPORT_MARKER" ]]; then
  export RADIOTEDU_BACKUP_PASSWORD="radiotedu"
  "$VENV_ROOT/bin/python" "$APP_ROOT/tools/import_portable_recovery.py" \
    --bundle-root "$BUNDLE_ROOT" \
    --data-root "$DATA_ROOT" \
    --user-config-root "$USER_ROOT" \
    --source-drive "H:" \
    --media-root "$MEDIA_ROOT"
  /usr/bin/touch "$IMPORT_MARKER"
  unset RADIOTEDU_BACKUP_PASSWORD
fi

PLIST_PATH="$("$VENV_ROOT/bin/python" "$APP_ROOT/tools/install_macos_launch_agent.py" \
  --app-root "$APP_ROOT" \
  --python "$VENV_ROOT/bin/python" \
  --data-root "$DATA_ROOT" \
  --user-config-root "$USER_ROOT" \
  --media-root "$MEDIA_ROOT" \
  --ffmpeg "$FFMPEG_BIN" \
  --ffprobe "$FFPROBE_BIN" \
  --port 18110)"

/bin/launchctl bootout "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "gui/$UID" "$PLIST_PATH"
/bin/launchctl kickstart -k "gui/$UID/com.radiotedu.onair"
sleep 4
/usr/bin/open "http://127.0.0.1:18110/?station_id=1#onair"
echo "RadioTEDU OnAir is installed and configured to start after login."
