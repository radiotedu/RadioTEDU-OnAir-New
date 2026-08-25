"""Install the per-user macOS launchd job for RadioTEDU OnAir."""

from __future__ import annotations

import argparse
import os
import plistlib
from pathlib import Path


LABEL = "com.radiotedu.onair"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--user-config-root", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18110)
    args = parser.parse_args()

    home = Path.home().resolve()
    app_root = args.app_root.expanduser().resolve()
    data_root = args.data_root.expanduser().resolve()
    user_root = args.user_config_root.expanduser().resolve()
    log_root = data_root / "Logs"
    log_root.mkdir(parents=True, exist_ok=True)
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LABEL}.plist"
    environment = {
        "CLEANROOM_PORT": str(args.port),
        "CLEANROOM_DB_PATH": str(data_root / "cleanroom.db"),
        "CLEANROOM_DATA_ROOT": str(data_root),
        "CLEANROOM_USER_CONFIG_ROOT": str(user_root),
        "CLEANROOM_CREDENTIAL_STORE_FILE": str(
            user_root / "secrets" / "station-credentials.json"
        ),
        "CLEANROOM_JWT_SECRET_FILE": str(user_root / "secrets" / "jwt-signing.key"),
        "CLEANROOM_OPEN_PANEL": "0",
        "CLEANROOM_SKIP_STARTUP_AI": "1",
        "CLEANROOM_DISABLE_LOCAL_PLAYBACK": "1",
        "RADIOTEDU_PROCESS_ISOLATED_WORKERS": "1",
        "RADIOTEDU_MEDIA_ROOT": str(args.media_root.expanduser().resolve(strict=False)),
        "RADIOTEDU_FFMPEG_PATH": str(args.ffmpeg.expanduser().resolve()),
        "RADIOTEDU_FFPROBE_PATH": str(args.ffprobe.expanduser().resolve()),
        "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            str(args.python.expanduser().resolve()),
            str(app_root / "run_cleanroom.py"),
        ],
        "WorkingDirectory": str(app_root),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_root / "macos-launchd.stdout.log"),
        "StandardErrorPath": str(log_root / "macos-launchd.stderr.log"),
    }
    temporary = plist_path.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.chmod(0o600)
    os.replace(temporary, plist_path)
    print(str(plist_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
