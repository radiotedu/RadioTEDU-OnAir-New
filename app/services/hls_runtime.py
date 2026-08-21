from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.runtime_paths import get_data_dir, resolve_binary


HLS_RADIOS = ("classic", "lofi", "cazz", "energize", "radio", "rock")
HLS_CODEC_PROFILE = "he_aac_v1_96_192"
HLS_LOW_BITRATE_KBPS = 96
HLS_HIGH_BITRATE_KBPS = 192
HLS_SAMPLE_RATE_HZ = 48_000
HLS_CHANNELS = 2
HLS_SEGMENT_DURATION_SECONDS = 6
HLS_PLAYLIST_SIZE = 10

_log = logging.getLogger(__name__)


class HlsRuntimeError(RuntimeError):
    """A safe, operator-facing HLS start/stop failure."""

    def __init__(self, error_code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.error_code = str(error_code)
        self.message = str(message)
        self.details = dict(details or {})


def _truthy(value: object, default: bool = False) -> bool:
    token = str(value if value is not None else default).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _output_root(settings: dict | None = None) -> Path:
    settings = dict(settings or {})
    configured = str(
        settings.get("hls_output_root")
        or os.getenv("RADIOTEDU_HLS_ROOT", "")
        or ""
    ).strip()
    return Path(configured) if configured else get_data_dir() / "hls"


def _playlist_paths(root: Path, radio: str) -> dict[str, Path]:
    radio_root = root / radio
    return {
        "master": radio_root / "master.m3u8",
        "low": radio_root / "low" / "index.m3u8",
        "high": radio_root / "high" / "index.m3u8",
    }


def build_ffmpeg_args(
    ffmpeg_path: str,
    source_url: str,
    output_root: str | Path,
    radio: str,
) -> list[str]:
    """Build the only supported HLS command: two HE-AAC v1 variants."""

    if radio not in HLS_RADIOS:
        raise ValueError(f"unsupported HLS radio: {radio}")
    root = Path(output_root) / radio
    segment_pattern = str(root / "%v" / "segment_%09d.ts")
    low_playlist = str(root / "%v" / "index.m3u8")
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-i",
        str(source_url),
        "-vn",
        "-map_metadata",
        "-1",
        "-map",
        "0:a:0",
        "-map",
        "0:a:0",
        "-c:a:0",
        "libfdk_aac",
        "-profile:a:0",
        "aac_he",
        "-b:a:0",
        f"{HLS_LOW_BITRATE_KBPS}k",
        "-ar:a:0",
        str(HLS_SAMPLE_RATE_HZ),
        "-ac:a:0",
        str(HLS_CHANNELS),
        "-c:a:1",
        "libfdk_aac",
        "-profile:a:1",
        "aac_he",
        "-b:a:1",
        f"{HLS_HIGH_BITRATE_KBPS}k",
        "-ar:a:1",
        str(HLS_SAMPLE_RATE_HZ),
        "-ac:a:1",
        str(HLS_CHANNELS),
        "-f",
        "hls",
        "-hls_time",
        str(HLS_SEGMENT_DURATION_SECONDS),
        "-hls_list_size",
        str(HLS_PLAYLIST_SIZE),
        "-hls_delete_threshold",
        "2",
        "-hls_start_number_source",
        "epoch",
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+program_date_time+temp_file",
        "-master_pl_name",
        "master.m3u8",
        "-master_pl_publish_rate",
        "1",
        "-var_stream_map",
        "a:0,name:low a:1,name:high",
        "-hls_segment_filename",
        segment_pattern,
        low_playlist,
    ]


class HlsRuntimeManager:
    """Own one HLS writer per RadioTEDU normal Icecast mount."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, object] = {}
        self._sources: dict[str, dict] = {}
        self._root = _output_root()
        self._last_error = ""
        self._last_error_code = ""

    @staticmethod
    def _configured_ffmpeg(settings: dict | None = None) -> tuple[str | None, str]:
        settings = dict(settings or {})
        configured = str(
            settings.get("hls_ffmpeg_path")
            or os.getenv("RADIOTEDU_FFMPEG_PATH", "")
            or ""
        ).strip().strip('"')
        if configured:
            path = Path(configured)
            if path.is_file():
                return str(path), ""
            return None, "hls_ffmpeg_not_found"
        return resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg"), ""

    def _runtime_available(self, settings: dict | None = None) -> tuple[bool, str, str]:
        ffmpeg, configured_error = self._configured_ffmpeg(settings)
        if configured_error:
            return False, configured_error, "The configured HLS FFmpeg path was not found."
        if not ffmpeg:
            return False, "ffmpeg_not_found", "FFmpeg binary was not found."
        try:
            from app.services.encoder_capabilities import inspect_he_aac_encoder

            capability = inspect_he_aac_encoder(ffmpeg)
        except Exception:
            capability = {"available": False, "error_code": "encoder_probe_failed"}
        if not capability.get("available"):
            return (
                False,
                str(capability.get("error_code") or "libfdk_aac_unavailable"),
                "The configured FFmpeg build does not provide libfdk_aac; HLS will not fall back to Opus.",
            )
        return True, "", ffmpeg

    @staticmethod
    def _source_url(source: dict) -> str:
        scheme = "https" if _truthy(source.get("tls"), False) else "http"
        host = str(source.get("host") or "127.0.0.1").strip()
        port = int(source.get("port") or 8000)
        mount = str(source.get("mount") or "").strip().lstrip("/")
        return f"{scheme}://{host}:{port}/{mount}"

    @staticmethod
    def _probe_source(source: dict, timeout_seconds: float = 5.0) -> int:
        url = HlsRuntimeManager._source_url(source)
        request = Request(url, headers={"User-Agent": "RadioTEDU-OnAir-HLS/1.0"})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(4096)
        except HTTPError as exc:
            raise HlsRuntimeError(
                "hls_source_http_error",
                f"HLS source {source.get('mount')} returned HTTP {exc.code}; no writer was started.",
                {"mount": source.get("mount"), "status": int(exc.code)},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HlsRuntimeError(
                "hls_source_unavailable",
                f"HLS source {source.get('mount')} did not return audio bytes; no writer was started.",
                {"mount": source.get("mount"), "reason": str(exc)[:180]},
            ) from exc
        if not payload:
            raise HlsRuntimeError(
                "hls_source_empty",
                f"HLS source {source.get('mount')} returned no audio bytes; no writer was started.",
                {"mount": source.get("mount")},
            )
        return len(payload)

    @staticmethod
    def _prepare_radio_root(root: Path, radio: str) -> None:
        radio_root = root / radio
        (radio_root / "low").mkdir(parents=True, exist_ok=True)
        (radio_root / "high").mkdir(parents=True, exist_ok=True)
        for path in (
            radio_root / "master.m3u8",
            radio_root / "low" / "index.m3u8",
            radio_root / "high" / "index.m3u8",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for segment in radio_root.glob("**/segment_*.ts"):
            try:
                segment.unlink()
            except FileNotFoundError:
                pass

    def _stop_unlocked(self, remove_outputs: bool = True) -> dict:
        stopped = 0
        for radio, process in list(self._processes.items()):
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                stopped += 1
            except (OSError, subprocess.SubprocessError):
                _log.exception("Could not stop HLS process for %s", radio)
            log_handle = self._logs.pop(radio, None)
            if log_handle is not None:
                try:
                    log_handle.close()
                except Exception:
                    pass
            if remove_outputs:
                paths = _playlist_paths(self._root, radio)
                for path in paths.values():
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        self._processes.clear()
        return {"stopped": stopped}

    def start(self, sources: list[dict], settings: dict | None = None) -> dict:
        settings = dict(settings or {})
        with self._lock:
            live = [radio for radio, process in self._processes.items() if process.poll() is None]
            if live:
                return self.status(settings=settings)
            available, error_code, ffmpeg_or_message = self._runtime_available(settings)
            if not available:
                self._last_error_code = error_code
                self._last_error = ffmpeg_or_message
                raise HlsRuntimeError(error_code, ffmpeg_or_message)
            by_radio = {
                str(source.get("radio") or "").strip().lower(): dict(source)
                for source in sources
                if str(source.get("radio") or "").strip().lower() in HLS_RADIOS
            }
            missing = [radio for radio in HLS_RADIOS if radio not in by_radio]
            if missing:
                raise HlsRuntimeError(
                    "hls_sources_incomplete",
                    "HLS requires all six RadioTEDU normal mounts before it starts.",
                    {"missing_radios": missing},
                )
            for radio in HLS_RADIOS:
                self._probe_source(by_radio[radio])

            root = _output_root(settings)
            root.mkdir(parents=True, exist_ok=True)
            self._root = root
            logs_root = root / "logs"
            logs_root.mkdir(parents=True, exist_ok=True)
            for radio in HLS_RADIOS:
                self._prepare_radio_root(root, radio)

            try:
                for radio in HLS_RADIOS:
                    source = by_radio[radio]
                    log_path = logs_root / f"{radio}.log"
                    log_handle = log_path.open("a", encoding="utf-8", errors="replace")
                    command = build_ffmpeg_args(
                        ffmpeg_or_message,
                        self._source_url(source),
                        root,
                        radio,
                    )
                    creationflags = (
                        getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    )
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                    )
                    self._logs[radio] = log_handle
                    self._processes[radio] = process
                    self._sources[radio] = source
            except (OSError, subprocess.SubprocessError) as exc:
                self._stop_unlocked(remove_outputs=True)
                raise HlsRuntimeError(
                    "hls_process_start_failed",
                    "FFmpeg could not start one or more HE-AAC HLS writers.",
                    {"reason": str(exc)[:180]},
                ) from exc

            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if all(_playlist_paths(root, radio)["master"].exists() for radio in HLS_RADIOS):
                    self._last_error = ""
                    self._last_error_code = ""
                    return self.status(settings=settings)
                if any(process.poll() is not None for process in self._processes.values()):
                    break
                time.sleep(0.25)

            logs = {}
            for radio in HLS_RADIOS:
                log_path = logs_root / f"{radio}.log"
                try:
                    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                        logs[radio] = handle.read()[-1000:]
                except OSError:
                    logs[radio] = ""
            self._stop_unlocked(remove_outputs=True)
            self._last_error_code = "hls_playlist_timeout"
            self._last_error = "FFmpeg started but did not produce all six master playlists."
            raise HlsRuntimeError(
                self._last_error_code,
                self._last_error,
                {"logs": logs},
            )

    def stop(self) -> dict:
        with self._lock:
            result = self._stop_unlocked(remove_outputs=True)
            self._last_error = ""
            self._last_error_code = ""
            return self.status()

    def status(self, settings: dict | None = None) -> dict:
        settings = dict(settings or {})
        with self._lock:
            for radio, process in list(self._processes.items()):
                if process.poll() is not None:
                    self._processes.pop(radio, None)
                    log_handle = self._logs.pop(radio, None)
                    if log_handle is not None:
                        try:
                            log_handle.close()
                        except Exception:
                            pass
            available, error_code, message_or_ffmpeg = self._runtime_available(settings)
            process_items = []
            for radio in HLS_RADIOS:
                process = self._processes.get(radio)
                paths = _playlist_paths(self._root, radio)
                process_items.append(
                    {
                        "radio": radio,
                        "mount": f"/{radio}",
                        "running": bool(process is not None and process.poll() is None),
                        "pid": int(process.pid) if process is not None else None,
                        "master_playlist": str(paths["master"]),
                        "playlist_active": bool(paths["master"].exists()),
                    }
                )
            running = bool(process_items) and all(item["running"] for item in process_items)
            playlist_active = bool(process_items) and all(item["playlist_active"] for item in process_items)
            status = "running" if running and playlist_active else ("error" if self._last_error else "stopped")
            return {
                "enabled": _truthy(settings.get("hls_enabled"), False),
                "runtime_available": bool(available),
                "status": status,
                "codec_profile": HLS_CODEC_PROFILE,
                "codec": "HE-AAC v1",
                "encoder": "libfdk_aac",
                "low_bitrate_kbps": HLS_LOW_BITRATE_KBPS,
                "high_bitrate_kbps": HLS_HIGH_BITRATE_KBPS,
                "sample_rate_hz": HLS_SAMPLE_RATE_HZ,
                "audio_channels": HLS_CHANNELS,
                "segment_duration_seconds": HLS_SEGMENT_DURATION_SECONDS,
                "playlist_size": HLS_PLAYLIST_SIZE,
                "playlist_active": playlist_active,
                "stored_disabled": not _truthy(settings.get("hls_enabled"), False),
                "output_root": str(self._root),
                "public_base_url": str(settings.get("hls_public_base_url") or ""),
                "source_mounts": [f"/{radio}" for radio in HLS_RADIOS],
                "processes": process_items,
                "last_error_code": self._last_error_code or ("" if available else error_code),
                "last_error": self._last_error or ("" if available else message_or_ffmpeg),
                "credentials_exposed": False,
            }


hls_runtime_manager = HlsRuntimeManager()
