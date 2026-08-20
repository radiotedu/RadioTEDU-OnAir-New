import re
import subprocess

from app.runtime_paths import require_binary


def parse_ffmpeg_dshow_audio_devices(stderr_output: str) -> list[str]:
    devices: list[str] = []
    pattern = re.compile(r'\[dshow @ .*?\]\s+"(.+?)"')
    for line in (stderr_output or "").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if candidate and not candidate.startswith("Alternative name"):
            devices.append(candidate)
    # Preserve order, de-duplicate.
    seen = set()
    out: list[str] = []
    for item in devices:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def list_output_devices(ffmpeg_bin: str | None = None) -> list[str]:
    resolved = ffmpeg_bin or require_binary("ffmpeg.exe")
    cmd = [ffmpeg_bin, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    cmd[0] = resolved
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    return parse_ffmpeg_dshow_audio_devices(proc.stderr)


def parse_ffmpeg_dshow_input_devices(stderr_output: str) -> list[str]:
    """Return friendly DirectShow audio-input labels only, never device IDs."""
    devices: list[str] = []
    in_audio_section = False
    pattern = re.compile(r'\[dshow @ .*?\]\s+"(.+?)"')
    for line in (stderr_output or "").splitlines():
        lowered = line.lower()
        if "directshow audio devices" in lowered:
            in_audio_section = True
            continue
        if "directshow video devices" in lowered:
            in_audio_section = False
            continue
        if not in_audio_section:
            continue
        match = pattern.search(line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if candidate and "alternative name" not in lowered:
            devices.append(candidate)
    return list(dict.fromkeys(devices))


def list_input_devices(
    ffmpeg_bin: str | None = None,
    *,
    timeout_seconds: float = 5.0,
) -> list[str]:
    """Enumerate physical DirectShow audio inputs without opening or recording them."""
    resolved = ffmpeg_bin or require_binary("ffmpeg.exe")
    proc = subprocess.run(
        [resolved, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(0.1, float(timeout_seconds)),
    )
    return parse_ffmpeg_dshow_input_devices(proc.stderr)
