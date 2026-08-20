"""Small, bounded listener probes shared by runtime-facing safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


_AUDIO_CONTENT_TYPES = {
    "application/ogg",
    "application/x-ogg",
    "video/ogg",
}


@dataclass(frozen=True)
class AudioStreamProbeResult:
    ok: bool
    status_code: int
    content_type: str
    sample_bytes: int
    reason: str


def _value(mapping: Any, key: str, default: Any = "") -> Any:
    try:
        value = mapping[key]
    except (KeyError, TypeError, IndexError):
        try:
            value = mapping.get(key, default)
        except (AttributeError, TypeError):
            value = default
    return default if value is None else value


def configured_listener_url(
    output: Any,
    station_settings: dict[str, Any] | None = None,
    public_base_url: str = "",
) -> str:
    protocol = str(_value(output, "source_protocol", "icecast") or "icecast").strip().lower()
    mount = str(_value(output, "icecast_mount") or "").strip()
    if not mount and protocol != "shoutcast":
        return ""
    if not mount:
        mount = "/"
    if not mount.startswith("/"):
        mount = f"/{mount}"
    base = str(public_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}{quote(mount, safe='/')}"

    host = str(_value(output, "icecast_host") or "").strip()
    try:
        port = int(_value(output, "icecast_port", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if not host or port <= 0:
        return ""
    settings = station_settings if isinstance(station_settings, dict) else {}
    if protocol == "shoutcast":
        try:
            port = int(settings.get("shoutcast_listener_port") or (port - 1))
        except (TypeError, ValueError):
            return ""
        if port <= 0:
            return ""
    tls_enabled = str(settings.get("icecast_tls_enabled", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{'https' if tls_enabled else 'http'}://{host}:{port}{quote(mount, safe='/')}"


def probe_audio_url(url: str, *, timeout: float = 1.5) -> AudioStreamProbeResult:
    target = str(url or "").strip()
    if not target:
        return AudioStreamProbeResult(False, 0, "", 0, "not_configured")
    request = Request(
        target,
        headers={
            "Icy-MetaData": "0",
            "Range": "bytes=0-0",
            "User-Agent": "RadioTEDU-OnAir-listener-verifier/1",
            "Accept": "audio/*, application/ogg;q=0.9",
            "Connection": "close",
        },
    )
    try:
        with urlopen(request, timeout=max(0.1, float(timeout))) as response:
            status = int(getattr(response, "status", 200) or 200)
            headers = getattr(response, "headers", {})
            content_type = str(headers.get("Content-Type", "") or "")
            normalized_type = content_type.split(";", 1)[0].strip().lower()
            if status not in {200, 206}:
                return AudioStreamProbeResult(
                    False, status, normalized_type, 0, "http_status"
                )
            if not (
                normalized_type.startswith("audio/")
                or normalized_type in _AUDIO_CONTENT_TYPES
            ):
                return AudioStreamProbeResult(
                    False, status, normalized_type, 0, "non_audio_content"
                )
            sample = bytes(response.read(1) or b"")
            if not sample:
                return AudioStreamProbeResult(
                    False, status, normalized_type, 0, "empty_audio_payload"
                )
            return AudioStreamProbeResult(
                True, status, normalized_type, len(sample), "audio_present"
            )
    except Exception:
        return AudioStreamProbeResult(False, 0, "", 0, "request_failed")


def probe_configured_audio(
    output: Any,
    station_settings: dict[str, Any] | None = None,
    public_base_url: str = "",
    *,
    timeout: float = 1.5,
) -> AudioStreamProbeResult:
    return probe_audio_url(
        configured_listener_url(output, station_settings, public_base_url),
        timeout=timeout,
    )
