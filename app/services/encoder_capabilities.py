from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from app.runtime_paths import resolve_binary


AAC_ENCODER = "aac"
AAC_PROFILE = "AAC-LC"
OPUS_ENCODER = "libopus"
OPUS_PROFILE = "Opus"


@lru_cache(maxsize=16)
def _inspect_ffmpeg(
    binary: str,
    modified_ns: int,
    encoder_name: str,
) -> dict[str, object]:
    del modified_ns
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "available": False,
            "error_code": "ffmpeg_encoder_probe_failed",
        }
    available = bool(
        result.returncode == 0
        and re.search(
            rf"(?m)^\s*A\S*\s+{re.escape(encoder_name)}\s",
            result.stdout or "",
        )
    )
    return {
        "available": available,
        "error_code": "" if available else f"{encoder_name}_encoder_unavailable",
    }


def inspect_aac_encoder() -> dict[str, object]:
    binary = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
    if not binary:
        result: dict[str, object] = {
            "available": False,
            "error_code": "ffmpeg_not_found",
        }
    else:
        try:
            modified_ns = Path(binary).stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        result = _inspect_ffmpeg(str(binary), modified_ns, AAC_ENCODER)
    return {
        "encoder": AAC_ENCODER,
        "profile": AAC_PROFILE,
        "available": bool(result.get("available")),
        "error_code": str(result.get("error_code") or ""),
        "credentials_exposed": False,
    }


def inspect_opus_encoder() -> dict[str, object]:
    binary = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
    if not binary:
        result: dict[str, object] = {
            "available": False,
            "error_code": "ffmpeg_not_found",
        }
    else:
        try:
            modified_ns = Path(binary).stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        result = _inspect_ffmpeg(str(binary), modified_ns, OPUS_ENCODER)
    return {
        "encoder": OPUS_ENCODER,
        "profile": OPUS_PROFILE,
        "available": bool(result.get("available")),
        "error_code": str(result.get("error_code") or ""),
        "credentials_exposed": False,
    }


# Compatibility name for older callers. The returned capability is deliberately
# labelled AAC-LC and never claims that the native encoder is AAC+ / HE-AAC.
inspect_he_aac_encoder = inspect_aac_encoder
