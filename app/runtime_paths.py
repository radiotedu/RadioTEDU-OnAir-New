import os
import shutil
import sys
from pathlib import Path


def _meipass_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    p = Path(str(base))
    return p if p.exists() else None


def _managed_binary(executable_name: str) -> Path | None:
    try:
        from app.dependency_bootstrap import managed_binary_path

        candidate = managed_binary_path(executable_name)
    except Exception:
        return None
    return candidate if candidate.exists() else None


def _configured_binary(executable_name: str) -> Path | None:
    """Return an explicitly configured local binary before bundled tools.

    The RadioTEDU HE-AAC build is intentionally kept outside the repository
    and selected with RADIOTEDU_FFMPEG_PATH.  This also prevents a packaged
    Opus-only ffmpeg from silently taking precedence over the operator's
    licensed/nonfree local FDK build.
    """
    env_name = {
        "ffmpeg.exe": "RADIOTEDU_FFMPEG_PATH",
        "ffmpeg": "RADIOTEDU_FFMPEG_PATH",
        "ffprobe.exe": "RADIOTEDU_FFPROBE_PATH",
        "ffprobe": "RADIOTEDU_FFPROBE_PATH",
    }.get(str(executable_name).lower())
    if not env_name:
        return None
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate.resolve() if candidate.is_file() else None


def resolve_binary_details(executable_name: str) -> dict[str, str | bool]:
    configured = _configured_binary(executable_name)
    if configured:
        return {
            "found": True,
            "path": str(configured),
            "source": "configured",
        }

    managed = _managed_binary(executable_name)
    if managed:
        return {
            "found": True,
            "path": str(managed),
            "source": "managed",
        }

    meipass = _meipass_dir()
    if meipass:
        bundled = meipass / executable_name
        if bundled.exists():
            return {
                "found": True,
                "path": str(bundled),
                "source": "bundle",
            }

    found = shutil.which(executable_name)
    if found:
        return {
            "found": True,
            "path": str(found),
            "source": "path",
        }

    return {
        "found": False,
        "path": "",
        "source": "",
    }


def get_data_dir() -> Path:
    """Get the data directory path."""
    try:
        from app.config import get_db_path

        # Runtime sidecars must follow the selected database instance. This
        # keeps development, tests, and parallel commissioned instances from
        # sharing process ledgers or music-history state.
        return get_db_path().parent
    except Exception:
        if getattr(sys, "frozen", False):
            program_data = os.getenv("PROGRAMDATA", "").strip()
            if program_data:
                return Path(program_data) / "RadioTEDU" / "OnAir"
        return Path(__file__).resolve().parents[1] / "data"


def resolve_binary(executable_name: str) -> str | None:
    details = resolve_binary_details(executable_name)
    path = str(details.get("path") or "")
    return path or None


def require_binary(executable_name: str) -> str:
    found = resolve_binary(executable_name)
    if found:
        return found
    raise FileNotFoundError(executable_name)
