import base64
import json
import logging
import os
import threading
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.metadata_policy import icecast_metadata_outputs
from app.audio.station_runtime import StationRuntime
from app.db import get_connection, init_db
from app.media_paths import resolve_runtime_media_path
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.services.track_naming import clean_album_metadata

_log = logging.getLogger("cleanroom.runtime_registry")
# Fire the first metadata push immediately, then retry quickly so correct
# now-playing lands in well under a second on a healthy origin. The previous
# tail (5s) meant a single early failure could delay accurate metadata by ~10s.
_ICECAST_METADATA_RETRY_DELAYS_SECONDS = (0.0, 0.3, 0.6, 1.2, 2.5)
# How often the background loop re-asserts each running station's current
# now-playing to the origin. The origin's HTTP metadata interface is unreliable,
# so a push at track-start can silently fail and leave now-playing stale for a
# whole song; periodic re-push bounds that staleness to this interval.
_ICECAST_METADATA_REFRESH_SECONDS = 20.0
_ICECAST_METADATA_BACKOFF_MAX_SECONDS = 300.0
_OUTPUT_RECOVERY_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
_OUTPUT_MONITOR_RECHECK_SECONDS = 15.0
_DEFAULT_LIVE_AUDIO_SETTINGS = {
    "program_music_mode": "normal",
    "mic_gain": 1.0,
    "music_gain": 1.0,
    "duck_level": 0.15,
}
_DEFAULT_LIVE_STATUS = {
    "live_input_enabled": False,
    "transmitting": False,
    "active_user": None,
    "receiving": False,
    "level_db": -60.0,
    "peak_db": -60.0,
    "buffer_bytes": 0,
}
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_BROADCAST_PROCESSING_PROFILES = {
    "balanced",
    "classical",
    "dense",
    "energize",
    "jazz",
    "lofi",
    "off",
    "pop",
    "rock",
    "transparent",
}


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUTHY_ENV_VALUES


def _icecast_metadata_disabled() -> bool:
    return _env_truthy("CLEANROOM_SKIP_ICECAST_METADATA")


def _local_playback_disabled() -> bool:
    return _env_truthy("CLEANROOM_DISABLE_LOCAL_PLAYBACK")


def _default_processing_profile_for_station(station_name: str) -> str:
    """Choose a conservative dynamics profile from the station identity."""

    name = str(station_name or "").strip().lower().replace("-", "")
    for marker, profile in (
        ("classical", "classical"),
        ("lofi", "lofi"),
        ("jazz", "jazz"),
        ("pop", "pop"),
        ("rock", "rock"),
        ("energize", "energize"),
        ("energetic", "energize"),
        ("electronic", "energize"),
    ):
        if marker in name:
            return profile
    return "balanced"


def _normalize_program_music_mode(raw) -> str:
    token = str(raw or "").strip().lower()
    if token in {"duck", "mute", "normal"}:
        return token
    return _DEFAULT_LIVE_AUDIO_SETTINGS["program_music_mode"]


def _normalize_gain(raw, default: float) -> float:
    try:
        return max(0.0, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return float(default)


def _normalize_duck_level(raw) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return float(_DEFAULT_LIVE_AUDIO_SETTINGS["duck_level"])


def _safe_int(raw, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)


def _truthy(raw, default: bool = False) -> bool:
    token = str(raw if raw is not None else default).strip().lower()
    if token in {"1", "true", "yes", "on", "public"}:
        return True
    if token in {"0", "false", "no", "off", "private"}:
        return False
    return bool(default)


def _station_genre(station_name: str) -> str:
    token = str(station_name or "").strip().lower()
    if "classic" in token or "flac" in token or "spark" in token:
        return "Classical"
    if "lo-fi" in token or "lofi" in token:
        return "Lo-Fi"
    if "jazz" in token or "cazz" in token:
        return "Jazz"
    if "pop" in token:
        return "Pop"
    if "rock" in token:
        return "Rock"
    if "energize" in token:
        return "Energetic"
    if "event" in token:
        return "Events"
    return "Radio"


def _station_stream_feature_settings(
    station_settings: dict,
    system_settings: dict,
    station_name: str,
) -> dict:
    genre = _station_genre(station_name)
    website_url = str(
        station_settings.get(
            "icecast_homepage_url",
            system_settings.get("radio_website_url", "https://radiotedu.com"),
        )
        or "https://radiotedu.com"
    ).strip()
    default_stream_name = {
        "Classical": "RadioTEDU Classic",
        "Lo-Fi": "RadioTEDU Lo-Fi",
        "Jazz": "RadioTEDU Jazz",
        "Pop": "RadioTEDU",
        "Energetic": "RadioTEDU Energize",
        "Rock": "RadioTEDU Rock",
        "Events": "RadioTEDU Events",
        "Radio": "RadioTEDU",
    }.get(genre, f"RadioTEDU {genre}")
    return {
        "icecast_stream_name": str(
            station_settings.get("icecast_stream_name", default_stream_name)
            or default_stream_name
        ).strip(),
        "icecast_description": str(
            station_settings.get(
                "icecast_description",
                f"{default_stream_name} live stream",
            )
            or ""
        ).strip(),
        "icecast_genre": str(
            station_settings.get("icecast_genre", genre) or genre
        ).strip(),
        "icecast_url": website_url,
        "icecast_public": _truthy(station_settings.get("icecast_public", "true"), True),
        "icecast_user_agent": str(
            station_settings.get(
                "icecast_user_agent",
                system_settings.get("icecast_user_agent", "RadioTEDU OnAir"),
            )
            or "RadioTEDU OnAir"
        ).strip(),
        "icecast_tls_enabled": _truthy(
            station_settings.get("icecast_tls_enabled", "false"),
            False,
        ),
        "icecast_legacy_source_enabled": _truthy(
            station_settings.get("icecast_legacy_source_enabled", "false"),
            False,
        ),
    }


def _ai_startup_status_snapshot(station_id: int, station_settings: dict) -> dict:
    if not _truthy(station_settings.get("ai_host_enabled", "false"), False):
        return {
            "ai_startup_state": "disabled",
            "ai_ready_intro_count": 0,
            "ai_required_intro_count": 0,
        }

    # Runtime status is called by the scheduler, liveness writer, watchdog and
    # UI polling.  It must never scan announcement cache files: a slow/removable
    # media volume previously blocked every caller here and prevented music
    # from starting.  AI prefetch persists its last readiness snapshot in
    # station settings; explicit AI diagnostics may perform a fresh scan.
    state = str(station_settings.get("startup_ai_readiness_state", "") or "")
    ready_count = _safe_int(station_settings.get("startup_ai_ready_intro_count", 0), 0)
    required_count = max(
        1,
        _safe_int(station_settings.get("startup_ai_required_intro_count", 1), 1),
    )
    return {
        "ai_startup_state": state or "warming",
        "ai_ready_intro_count": ready_count,
        "ai_required_intro_count": required_count,
    }


def _live_audio_settings_snapshot(repo: SettingsRepository, station_id: int) -> dict:
    station_settings = repo.get_station(int(station_id))
    return {
        "program_music_mode": _normalize_program_music_mode(
            station_settings.get("program_music_mode")
        ),
        "mic_gain": _normalize_gain(
            station_settings.get("program_mic_gain"),
            _DEFAULT_LIVE_AUDIO_SETTINGS["mic_gain"],
        ),
        "music_gain": _normalize_gain(
            station_settings.get("program_music_gain"),
            _DEFAULT_LIVE_AUDIO_SETTINGS["music_gain"],
        ),
        "duck_level": _normalize_duck_level(
            station_settings.get("program_duck_level")
        ),
    }


_MOJIBAKE_MARKERS = ("Ã", "Â", "â", "�")


def _repair_mojibake_text(value: str) -> str:
    """Repair double-encoded UTF-8 (mojibake) the same way the UI does.

    The frontend's repairMojibakeText() shows clean titles, but the Icecast
    now-playing push previously sent the raw stored value, so the public stream
    advertised garbled text (e.g. "RÃ³zsa RÅ¯Å¾iÄkovÃ¡"). This mirrors the JS
    decodeURIComponent(escape(raw)) trick: reinterpret the chars as latin-1
    bytes and decode as UTF-8, but only keep the result if it removes the
    mojibake markers.
    """
    raw = str(value or "").strip()
    if not raw or not any(marker in raw for marker in _MOJIBAKE_MARKERS):
        return raw
    try:
        repaired = raw.encode("latin-1").decode("utf-8").strip()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw
    if repaired and not any(marker in repaired for marker in _MOJIBAKE_MARKERS):
        return repaired
    return raw


def _compose_now_playing(title: str, artist: str, album: str = "") -> str:
    clean_title = _repair_mojibake_text(str(title or "").strip())
    clean_artist = _repair_mojibake_text(str(artist or "").strip())
    clean_album = _repair_mojibake_text(clean_album_metadata(album))
    if clean_title and clean_artist:
        song = f"{clean_artist} - {clean_title}"
    else:
        song = clean_title or clean_artist
    if clean_album and clean_album.casefold() not in {
        clean_title.casefold(),
        clean_artist.casefold(),
    }:
        song = f"{song} ({clean_album})" if song else clean_album
    return song


def _metadata_base_url(
    host: str, port: int, tls_enabled: bool = False
) -> str:
    clean_host = str(host or "").strip().rstrip("/")
    if "://" in clean_host:
        return clean_host
    scheme = "https" if bool(tls_enabled) else "http"
    return f"{scheme}://{clean_host}:{int(port)}"


def _send_icecast_metadata(
    cfg: StationPipelineConfig,
    *,
    timeout_seconds: float = 1.0,
    should_continue=None,
    on_result=None,
) -> bool:
    if _icecast_metadata_disabled():
        return False
    if not bool(cfg.icecast_enabled):
        return False
    song = _compose_now_playing(
        cfg.stream_title,
        cfg.stream_artist,
        getattr(cfg, "stream_album", ""),
    )
    if not song:
        return False

    outputs = icecast_metadata_outputs(cfg)
    attempted = False
    all_sent = True
    for output in outputs:
        if callable(should_continue) and not should_continue():
            return False
        host = str(output.get("icecast_host") or cfg.icecast_host or "").strip()
        if not host:
            continue
        attempted = True
        port = _safe_int(output.get("icecast_port", cfg.icecast_port), int(cfg.icecast_port))
        tls_enabled = _truthy(
            output.get("icecast_tls_enabled", cfg.icecast_tls_enabled),
            bool(cfg.icecast_tls_enabled),
        )
        base_url = _metadata_base_url(host, port, tls_enabled).rstrip("/")
        mount = str(output.get("icecast_mount") or cfg.icecast_mount or "").strip() or "/stream"
        if not mount.startswith("/"):
            mount = f"/{mount}"

        request_url = (
            f"{base_url}/admin/metadata"
            f"?mode=updinfo&mount={quote(mount, safe='/')}&song={quote(song, safe='')}"
        )
        user = str(output.get("icecast_user") or cfg.icecast_user or "").strip()
        password = str(output.get("icecast_password", cfg.icecast_password) or "")
        auth_token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request = Request(
            request_url,
            headers={
                "Authorization": f"Basic {auth_token}",
                "User-Agent": "RadioTEDU OnAir",
            },
        )
        # The origin (TinyIce) / the network path to it intermittently resets
        # fresh connections (WinError 10054 / RemoteDisconnected), so a single
        # metadata push often fails transiently. Retry a few times with a short
        # gap before giving up, so now-playing lands promptly instead of waiting
        # for the next scheduled retry (or showing stale text to listeners).
        last_exc = None
        output_sent = False
        attempts = 0
        for attempt in range(4):
            if callable(should_continue) and not should_continue():
                return False
            attempts = attempt + 1
            try:
                with urlopen(request, timeout=max(0.25, float(timeout_seconds))):
                    output_sent = True
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < 3:
                    if callable(should_continue) and not should_continue():
                        return False
                    time.sleep(0.2)
        if not output_sent:
            all_sent = False
        if callable(on_result):
            try:
                on_result(
                    {
                        "ok": output_sent,
                        "mount": mount,
                        "host": host,
                        "port": port,
                        "scheme": (
                            "https"
                            if base_url.lower().startswith("https://")
                            else "http"
                        ),
                        "attempts": attempts,
                        "status": (
                            getattr(last_exc, "code", None)
                            if last_exc is not None
                            else 200
                        ),
                        "error": (
                            str(last_exc)[:240] if last_exc is not None else ""
                        ),
                    }
                )
            except Exception:
                pass
        if last_exc is not None:
            _log.warning(
                "Icecast metadata push failed mount=%s host=%s status=%s err=%s",
                mount,
                host,
                getattr(last_exc, "code", None),
                last_exc,
            )
    return attempted and all_sent


def _extra_icecast_outputs(settings: dict, station_id: int, row) -> tuple[dict, ...]:
    raw = settings.get(f"station_{int(station_id)}_extra_icecast_outputs", "")
    if not raw:
        return ()
    try:
        values = json.loads(str(raw))
    except Exception:
        return ()
    if not isinstance(values, list):
        return ()
    outputs = []
    station_is_lofi = int(station_id) == 2
    for value in values:
        if not isinstance(value, dict) or not _truthy(value.get("enabled"), True):
            continue
        mount = str(value.get("icecast_mount") or value.get("mount") or "").strip()
        if not mount:
            continue
        if not mount.startswith("/"):
            mount = f"/{mount}"
        profile = str(
            value.get("stream_codec_profile")
            or value.get("codec")
            or row["stream_codec_profile"]
        )
        bitrate = _safe_int(
            value.get(
                "stream_bitrate_kbps",
                value.get("bitrate_kbps", row["stream_bitrate_kbps"]),
            ),
            int(row["stream_bitrate_kbps"] or 192),
        )
        # Normal/high and low are fixed listener contracts. Persisted legacy
        # profiles self-heal in memory; FLAC branches remain byte-for-byte on
        # their existing lossless profile.
        quality = str(value.get("quality") or "").strip().lower()
        if quality == "high" or mount.lower().endswith("-high"):
            profile = "aac_low_192"
            bitrate = 192
        elif quality == "low" or mount.lower().endswith("-low"):
            profile = "aac_he_v2_64"
            bitrate = 64
        outputs.append(
            {
                "enabled": True,
                "name": str(value.get("name") or mount),
                "icecast_host": str(value.get("icecast_host") or value.get("host") or row["icecast_host"]),
                "icecast_port": _safe_int(
                    value.get("icecast_port", value.get("port", row["icecast_port"])),
                    int(row["icecast_port"]),
                ),
                "icecast_mount": mount,
                "icecast_user": str(value.get("icecast_user") or value.get("user") or row["icecast_user"]),
                "icecast_password": str(
                    value.get("icecast_password", value.get("password", row["icecast_password"])) or ""
                ),
                "stream_codec_profile": profile,
                "stream_bitrate_kbps": bitrate,
                "icecast_stream_name": str(value.get("icecast_stream_name") or value.get("name") or ""),
                "icecast_description": str(value.get("icecast_description") or ""),
                "icecast_genre": str(value.get("icecast_genre") or ""),
                "icecast_url": str(value.get("icecast_url") or ""),
                "icecast_public": _truthy(value.get("icecast_public", "true"), True),
                "metadata_suppressed": station_is_lofi or _truthy(
                    value.get("metadata_suppressed", "false"), False
                ),
                "icecast_user_agent": str(value.get("icecast_user_agent") or "RadioTEDU OnAir"),
                "icecast_tls_enabled": _truthy(value.get("icecast_tls_enabled", "false"), False),
            }
        )
    return tuple(outputs)

class StationRuntimeRegistry:
    def __init__(
        self,
        runtime_factory=None,
        live_mic_registry=None,
        guest_audio_registry=None,
    ):
        self._runtime_factory = runtime_factory or (
            lambda station_id: StationRuntime(
                station_id=station_id,
                live_mic_registry=live_mic_registry,
                guest_audio_registry=guest_audio_registry,
                live_settings_provider=self.get_live_audio_settings,
            )
        )
        self._live_mic_registry = live_mic_registry
        self._guest_audio_registry = guest_audio_registry
        self._runtimes: dict[int, StationRuntime] = {}
        self._required_outputs: dict[int, dict[str, bool]] = {}
        self._live_audio_settings_cache: dict[int, dict] = {}
        self._metadata_generations: dict[int, int] = {}
        self._metadata_lock = threading.Lock()
        # Latest now-playing config per station, re-pushed periodically so that a
        # metadata push that failed at track-start self-heals once the origin's
        # (unreliable) HTTP interface is reachable again.
        self._last_metadata_cfg: dict[int, StationPipelineConfig] = {}
        # Last now-playing string CONFIRMED delivered to the origin per station, so
        # the refresh loop only re-pushes what is actually missing/stale and keeps
        # retrying a failed push until it lands.
        self._metadata_sent: dict[int, str] = {}
        self._metadata_sent_lock = threading.Lock()
        self._metadata_delivery_status: dict[int, dict] = {}
        self._metadata_delivery_status_lock = threading.Lock()
        self._metadata_retry_state: dict[int, dict] = {}
        self._metadata_retry_state_lock = threading.Lock()
        self._metadata_worker_events: dict[int, threading.Event] = {}
        self._metadata_worker_started: set[int] = set()
        self._metadata_worker_lock = threading.Lock()
        self._recovery_lock = threading.RLock()
        self._recovery_state: dict[int, dict] = {}
        self._operation_locks_lock = threading.Lock()
        self._operation_locks: dict[int, threading.RLock] = {}
        if self._live_mic_registry is not None:
            register_listener = getattr(self._live_mic_registry, "register_listener", None)
            if callable(register_listener):
                register_listener(self._handle_live_mic_event)

    def _operation_lock(self, station_id: int) -> threading.RLock:
        sid = int(station_id)
        with self._operation_locks_lock:
            lock = self._operation_locks.get(sid)
            if lock is None:
                lock = threading.RLock()
                self._operation_locks[sid] = lock
            return lock

    def _ensure_metadata_worker(self, station_id: int) -> threading.Event:
        sid = int(station_id)
        with self._metadata_worker_lock:
            event = self._metadata_worker_events.get(sid)
            if event is None:
                event = threading.Event()
                self._metadata_worker_events[sid] = event
            if sid not in self._metadata_worker_started:
                self._metadata_worker_started.add(sid)
                threading.Thread(
                    target=self._metadata_station_worker,
                    args=(sid, event),
                    name=f"icecast-metadata-{sid}",
                    daemon=True,
                ).start()
            return event

    def _wake_metadata_worker(self, station_id: int) -> None:
        self._ensure_metadata_worker(int(station_id)).set()

    def _current_metadata_generation(self, station_id: int) -> int:
        with self._metadata_lock:
            return int(self._metadata_generations.get(int(station_id), 0))

    def _schedule_latest_metadata_update(self, station_id: int) -> None:
        sid = int(station_id)
        cfg = self._last_metadata_cfg.get(sid)
        if cfg is not None:
            self._schedule_icecast_metadata_update(
                sid, cfg, self._current_metadata_generation(sid)
            )

    def _start_metadata_refresh_loop(self) -> None:
        if getattr(self, "_metadata_refresh_started", False):
            return
        self._metadata_refresh_started = True
        thread = threading.Thread(
            target=self._metadata_refresh_worker,
            name="icecast-metadata-refresh",
            daemon=True,
        )
        thread.start()

    def _push_metadata_now(
        self, station_id: int, cfg, generation: int | None = None
    ) -> bool:
        """Push now-playing and record it as delivered on success."""
        sid = int(station_id)
        song = _compose_now_playing(
            cfg.stream_title,
            cfg.stream_artist,
            getattr(cfg, "stream_album", ""),
        )
        if not song:
            return False
        if generation is not None and not self._metadata_generation_current(
            sid, generation
        ):
            return False

        def generation_current() -> bool:
            return generation is None or self._metadata_generation_current(
                sid, generation
            )

        results: list[dict] = []
        sent = _send_icecast_metadata(
            cfg,
            should_continue=generation_current,
            on_result=results.append,
        )
        if generation is not None and not self._metadata_generation_current(
            sid, generation
        ):
            with self._metadata_sent_lock:
                self._metadata_sent.pop(sid, None)
            self._schedule_latest_metadata_update(sid)
            return False
        if sent:
            with self._metadata_sent_lock:
                self._metadata_sent[sid] = song
        self._record_metadata_retry_outcome(sid, sent)
        if results:
            self._record_metadata_delivery_status(sid, song, sent, results)
        return sent

    def _record_metadata_retry_outcome(self, station_id: int, ok: bool) -> None:
        sid = int(station_id)
        with self._metadata_retry_state_lock:
            if ok:
                self._metadata_retry_state.pop(sid, None)
                return
            previous = dict(self._metadata_retry_state.get(sid, {}))
            failures = min(16, int(previous.get("failures", 0)) + 1)
            delay = min(
                _ICECAST_METADATA_BACKOFF_MAX_SECONDS,
                _ICECAST_METADATA_REFRESH_SECONDS
                * (2 ** max(0, failures - 1)),
            )
            self._metadata_retry_state[sid] = {
                "failures": failures,
                "next_retry_monotonic": time.monotonic() + delay,
            }

    def _metadata_retry_ready(self, station_id: int) -> bool:
        with self._metadata_retry_state_lock:
            state = dict(self._metadata_retry_state.get(int(station_id), {}))
        return time.monotonic() >= float(
            state.get("next_retry_monotonic", 0.0)
        )

    def _metadata_retry_payload(self, station_id: int) -> dict:
        with self._metadata_retry_state_lock:
            state = dict(self._metadata_retry_state.get(int(station_id), {}))
        retry_in = max(
            0.0,
            float(state.get("next_retry_monotonic", 0.0))
            - time.monotonic(),
        )
        return {
            "failure_count": int(state.get("failures", 0)),
            "retry_in_seconds": round(retry_in, 1),
        }

    def _record_metadata_delivery_status(
        self, station_id: int, song: str, ok: bool, outputs: list[dict]
    ) -> None:
        sanitized = [
            {
                "ok": bool(output.get("ok")),
                "mount": str(output.get("mount") or ""),
                "host": str(output.get("host") or ""),
                "port": int(output.get("port") or 0),
                "scheme": str(output.get("scheme") or "http"),
                "attempts": int(output.get("attempts") or 0),
                "status": output.get("status"),
                "error": str(output.get("error") or "")[:240],
            }
            for output in outputs
        ]
        last_error = next(
            (
                output["error"]
                for output in sanitized
                if not output["ok"] and output["error"]
            ),
            "",
        )
        with self._metadata_delivery_status_lock:
            self._metadata_delivery_status[int(station_id)] = {
                "ok": bool(ok),
                "song": str(song or ""),
                "updated_at": time.time(),
                "last_error": last_error,
                "outputs": sanitized,
                "retry": self._metadata_retry_payload(station_id),
            }

    def _metadata_delivery_payload(self, station_id: int) -> dict:
        with self._metadata_delivery_status_lock:
            payload = dict(
                self._metadata_delivery_status.get(int(station_id), {})
            )
        if isinstance(payload.get("outputs"), list):
            payload["outputs"] = [
                dict(output)
                for output in payload["outputs"]
                if isinstance(output, dict)
            ]
        return payload

    def _metadata_station_worker(
        self, station_id: int, event: threading.Event
    ) -> None:
        sid = int(station_id)
        while True:
            triggered = event.wait(_ICECAST_METADATA_REFRESH_SECONDS)
            event.clear()
            try:
                self._push_latest_metadata_for_station(
                    sid, event, require_running=not bool(triggered)
                )
            except Exception:
                _log.exception(
                    "Metadata worker failed for station_id=%s", sid
                )

    def _push_latest_metadata_for_station(
        self,
        station_id: int,
        event: threading.Event | None = None,
        *,
        require_running: bool = False,
    ) -> None:
        sid = int(station_id)
        if require_running and not self._metadata_retry_ready(sid):
            return
        while True:
            restart_for_newer = False
            for delay in _ICECAST_METADATA_RETRY_DELAYS_SECONDS:
                if delay > 0:
                    if event is not None and event.wait(delay):
                        event.clear()
                        restart_for_newer = True
                        break
                    if event is None:
                        time.sleep(delay)
                cfg = self._last_metadata_cfg.get(sid)
                if cfg is None or not bool(
                    getattr(cfg, "icecast_enabled", False)
                ):
                    return
                if require_running:
                    runtime = self._runtimes.get(sid)
                    if runtime is None:
                        return
                    is_running = getattr(runtime, "is_running", None)
                    if callable(is_running) and not is_running():
                        return
                song = _compose_now_playing(
                    cfg.stream_title,
                    cfg.stream_artist,
                    getattr(cfg, "stream_album", ""),
                )
                if not song:
                    return
                with self._metadata_sent_lock:
                    if self._metadata_sent.get(sid) == song:
                        return
                generation = self._current_metadata_generation(sid)
                if self._push_metadata_now(sid, cfg, generation):
                    return
                if not self._metadata_generation_current(sid, generation):
                    restart_for_newer = True
                    break
            if not restart_for_newer:
                return

    def _metadata_refresh_worker(self) -> None:
        while True:
            time.sleep(_ICECAST_METADATA_REFRESH_SECONDS)
            if _icecast_metadata_disabled():
                continue
            try:
                items = list(self._last_metadata_cfg.items())
            except Exception:
                items = []
            for station_id, cfg in items:
                try:
                    if not bool(getattr(cfg, "icecast_enabled", False)):
                        continue
                    runtime = self._runtimes.get(int(station_id))
                    if runtime is None:
                        continue
                    is_running = getattr(runtime, "is_running", None)
                    if callable(is_running) and not is_running():
                        continue
                    song = _compose_now_playing(
                        cfg.stream_title,
                        cfg.stream_artist,
                        getattr(cfg, "stream_album", ""),
                    )
                    if not song:
                        continue
                    with self._metadata_sent_lock:
                        already_delivered = self._metadata_sent.get(int(station_id)) == song
                    if already_delivered:
                        continue  # current now-playing already confirmed on the origin
                    if not self._metadata_retry_ready(int(station_id)):
                        continue
                    # Missing or stale on the origin (or a prior push failed) -> push
                    # and keep retrying on each cycle until it lands.
                    self._push_metadata_now(station_id, cfg)
                except Exception:
                    continue

    def _get_or_create(self, station_id: int):
        if station_id not in self._runtimes:
            try:
                runtime = self._runtime_factory(int(station_id))
            except TypeError:
                runtime = self._runtime_factory()
            self._configure_runtime(runtime, station_id)
            self._runtimes[station_id] = runtime
        return self._runtimes[station_id]

    def _next_metadata_generation(self, station_id: int) -> int:
        sid = int(station_id)
        with self._metadata_lock:
            generation = int(self._metadata_generations.get(sid, 0)) + 1
            self._metadata_generations[sid] = generation
            return generation

    def _metadata_generation_current(self, station_id: int, generation: int) -> bool:
        with self._metadata_lock:
            return int(self._metadata_generations.get(int(station_id), 0)) == int(generation)

    def _schedule_icecast_metadata_update(
        self,
        station_id: int,
        cfg: StationPipelineConfig,
        generation: int,
    ) -> None:
        if not bool(cfg.icecast_enabled):
            return
        if not _compose_now_playing(
            cfg.stream_title,
            cfg.stream_artist,
            getattr(cfg, "stream_album", ""),
        ):
            return
        self._wake_metadata_worker(station_id)

    def get_sound_effect_player(self, station_id: int):
        runtime = self._runtimes.get(int(station_id))
        if runtime is None:
            return None
        return getattr(runtime, "sound_effect_player", None)

    def _configure_runtime(self, runtime, station_id: int) -> None:
        configure = getattr(runtime, "configure_live_context", None)
        if callable(configure):
            configure(
                station_id=int(station_id),
                live_mic_registry=self._live_mic_registry,
                guest_audio_registry=self._guest_audio_registry,
                live_settings_provider=self.get_live_audio_settings,
            )
            return
        if hasattr(runtime, "station_id"):
            runtime.station_id = int(station_id)
        if hasattr(runtime, "live_mic_registry"):
            runtime.live_mic_registry = self._live_mic_registry
        if hasattr(runtime, "guest_audio_registry"):
            runtime.guest_audio_registry = self._guest_audio_registry
        if hasattr(runtime, "live_settings_provider"):
            runtime.live_settings_provider = self.get_live_audio_settings

    def refresh_live_audio_settings(self, station_id: int) -> dict:
        sid = int(station_id)
        init_db()
        conn = get_connection()
        try:
            snapshot = _live_audio_settings_snapshot(SettingsRepository(conn), sid)
        finally:
            conn.close()
        self._live_audio_settings_cache[sid] = dict(snapshot)
        return dict(snapshot)

    def refresh_output_settings(self, station_id: int) -> dict:
        """Hot-apply extra output branches while preserving primary playout."""
        sid = int(station_id)
        with self._operation_lock(sid):
            runtime = self._runtimes.get(sid)
            if runtime is None:
                return self.status(sid)
            init_db()
            conn = get_connection()
            try:
                settings = SettingsRepository(conn).get_system()
                row = StationOutputRepository(conn).get(sid)
                outputs = (
                    _extra_icecast_outputs(settings, sid, row)
                    if row is not None
                    else ()
                )
            finally:
                conn.close()
            refresh = getattr(runtime, "refresh_extra_icecast_outputs", None)
            if not callable(refresh):
                raise RuntimeError("runtime_output_refresh_unavailable")
            result = dict(refresh(outputs) or {})
            required = dict(self._required_outputs.get(sid) or {})
            required = {
                branch: enabled
                for branch, enabled in required.items()
                if not str(branch).startswith("icecast:")
            }
            required.update(
                {
                    f"icecast:{str(output.get('icecast_mount') or '').strip()}": True
                    for output in outputs
                }
            )
            self._required_outputs[sid] = required
            result["status"] = self.status(sid)
            return result

    def get_live_audio_settings(self, station_id: int | None = None) -> dict:
        sid = int(station_id or 1)
        cached = self._live_audio_settings_cache.get(sid)
        if cached is not None:
            return dict(cached)
        try:
            return self.refresh_live_audio_settings(sid)
        except Exception:
            return dict(_DEFAULT_LIVE_AUDIO_SETTINGS)

    def _live_snapshot(self, station_id: int) -> dict:
        if self._live_mic_registry is None:
            return dict(_DEFAULT_LIVE_STATUS)
        snapshot = getattr(self._live_mic_registry, "snapshot", None)
        if not callable(snapshot):
            return dict(_DEFAULT_LIVE_STATUS)
        try:
            payload = snapshot(int(station_id))
        except Exception:
            return dict(_DEFAULT_LIVE_STATUS)
        result = dict(_DEFAULT_LIVE_STATUS)
        result.update(dict(payload or {}))
        return result

    def _runtime_live_fields(self, station_id: int, backend: str = "none") -> dict:
        snapshot = self._live_snapshot(station_id)
        settings = self.get_live_audio_settings(station_id)
        return {
            "live_input_enabled": bool(snapshot.get("live_input_enabled")),
            "live_mic_active": bool(snapshot.get("transmitting")),
            "live_mic_user": snapshot.get("active_user"),
            "live_mic_receiving": bool(snapshot.get("receiving")),
            "live_mic_level_db": float(snapshot.get("level_db", -60.0)),
            "live_mic_peak_db": float(snapshot.get("peak_db", -60.0)),
            "live_mic_buffer_bytes": int(snapshot.get("buffer_bytes", 0)),
            "program_music_mode": str(
                settings.get(
                    "program_music_mode",
                    _DEFAULT_LIVE_AUDIO_SETTINGS["program_music_mode"],
                )
            ),
            "live_mix_backend": "active" if str(backend or "") == "live-mix" else "inactive",
        }

    def _handle_live_mic_event(self, event_type: str, station_id: int, _snapshot: dict) -> None:
        if str(event_type or "").strip().lower() != "start":
            return
        runtime = self._runtimes.get(int(station_id))
        if runtime is None:
            return
        try:
            self.refresh_live_audio_settings(station_id)
        except Exception:
            pass
        promote = getattr(runtime, "promote_live_mix", None)
        if callable(promote):
            try:
                promote()
            except Exception as exc:
                _log.warning(
                    "Live mic promotion failed for station %s: %s",
                    station_id,
                    exc,
                )

    def promote_live_mix(self, station_id: int, *, force: bool = False) -> bool:
        runtime = self._runtimes.get(int(station_id))
        if runtime is None:
            return False
        promote = getattr(runtime, "promote_live_mix", None)
        if not callable(promote):
            return False
        try:
            promote(force=bool(force))
        except TypeError:
            promote()
        return True

    @staticmethod
    def _default_output_settings(station_id: int) -> dict:
        sid = int(station_id)
        return {
            "station_id": sid,
            "local_output_enabled": True,
            "output_device_id": "",
            "icecast_enabled": False,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": f"/station{sid}",
            "icecast_user": "source",
            "icecast_password": "",
            "output_gain_db": 0.0,
            "stream_codec_profile": "aac_low_192",
            "stream_bitrate_kbps": 192,
            "source_protocol": "icecast",
        }

    @classmethod
    def _is_legacy_implicit_icecast_default(cls, row, station_id: int) -> bool:
        if not row:
            return False
        sid = int(station_id)
        try:
            gain = float(row["output_gain_db"] or 0.0)
            profile = str(row["stream_codec_profile"] or "").strip().lower()
            bitrate = int(row["stream_bitrate_kbps"] or 0)
        except (TypeError, ValueError):
            return False
        return (
            bool(row["local_output_enabled"]) is False
            and str(row["output_device_id"] or "").strip() == ""
            and bool(row["icecast_enabled"]) is True
            and str(row["icecast_host"] or "").strip() == "127.0.0.1"
            and int(row["icecast_port"] or 0) == 8000
            and str(row["icecast_mount"] or "").strip() == f"/station{sid}"
            and str(row["icecast_user"] or "").strip() == "source"
            and str(row["icecast_password"] or "") in {"", "hack" + "me"}
            and abs(gain) <= 1e-9
            and (profile, bitrate) in {("aac_plus_196", 196), ("opus_96", 96)}
        )

    @classmethod
    def _resolve_output_settings(cls, conn, station_id: int, repo):
        current = repo.get(int(station_id))
        if current is not None and not cls._is_legacy_implicit_icecast_default(
            current, int(station_id)
        ):
            return current
        row = cls._sync_output_settings(conn, station_id, repo)
        if row is not None:
            return row

        defaults = cls._default_output_settings(station_id)
        repo.upsert(
            station_id=int(defaults["station_id"]),
            local_output_enabled=bool(defaults["local_output_enabled"]),
            output_device_id=str(defaults["output_device_id"]),
            icecast_enabled=bool(defaults["icecast_enabled"]),
            icecast_host=str(defaults["icecast_host"]),
            icecast_port=int(defaults["icecast_port"]),
            icecast_mount=str(defaults["icecast_mount"]),
            icecast_user=str(defaults["icecast_user"]),
            icecast_password=str(defaults["icecast_password"]),
            output_gain_db=float(defaults["output_gain_db"]),
            stream_codec_profile=str(defaults["stream_codec_profile"]),
            stream_bitrate_kbps=int(defaults["stream_bitrate_kbps"]),
        )
        return repo.get(int(station_id)) or defaults

    @staticmethod
    def _sync_output_settings(conn, station_id: int, repo) -> dict | None:
        """Sync station_outputs from station_settings so that output_mode
        (speaker vs icecast) is always current before starting the runtime.

        Only writes when station_settings contains an explicit output_mode;
        if the user never configured output_mode we leave station_outputs
        untouched so direct API callers and tests are not surprised."""
        sid = int(station_id)
        station_settings = SettingsRepository(conn).get_station(sid)
        current = repo.get(sid)

        if "output_mode" not in station_settings:
            if current and StationRuntimeRegistry._is_legacy_implicit_icecast_default(
                current, sid
            ):
                defaults = StationRuntimeRegistry._default_output_settings(sid)
                repo.upsert(
                    station_id=sid,
                    local_output_enabled=bool(defaults["local_output_enabled"]),
                    output_device_id=str(defaults["output_device_id"]),
                    icecast_enabled=bool(defaults["icecast_enabled"]),
                    icecast_host=str(defaults["icecast_host"]),
                    icecast_port=int(defaults["icecast_port"]),
                    icecast_mount=str(defaults["icecast_mount"]),
                    icecast_user=str(defaults["icecast_user"]),
                    icecast_password=str(defaults["icecast_password"]),
                    output_gain_db=float(defaults["output_gain_db"]),
                    stream_codec_profile=str(defaults["stream_codec_profile"]),
                    stream_bitrate_kbps=int(defaults["stream_bitrate_kbps"]),
                    source_protocol=str(defaults["source_protocol"]),
                )
                return repo.get(sid) or defaults
            return current

        current_local = bool(current["local_output_enabled"]) if current else False
        current_device = str(current["output_device_id"]) if current else ""
        current_icecast = bool(current["icecast_enabled"]) if current else True
        current_host = str(current["icecast_host"]) if current else "127.0.0.1"
        current_port = int(current["icecast_port"]) if current else 8000
        current_mount = str(current["icecast_mount"]) if current else f"/station{sid}"
        current_user = str(current["icecast_user"]) if current else "source"
        current_pass = str(current["icecast_password"]) if current else ""
        current_gain = float(current["output_gain_db"]) if current else 0.0
        current_profile = str(current["stream_codec_profile"]) if current else "aac_low_192"
        current_bitrate = int(current["stream_bitrate_kbps"]) if current else 192
        current_protocol = str(current["source_protocol"] or "icecast") if current else "icecast"

        mode = str(station_settings.get("output_mode", "speaker") or "speaker").strip().lower()
        raw_monitor = station_settings.get("speaker_monitor_enabled", True)
        if isinstance(raw_monitor, bool):
            speaker_monitor = raw_monitor
        else:
            speaker_monitor = str(raw_monitor or "").strip().lower() in {"1", "true", "yes", "on"}

        if mode in {"icecast", "shoutcast"}:
            icecast_enabled = True
            local_enabled = bool(speaker_monitor)
        else:
            icecast_enabled = False
            local_enabled = True

        def _safe_int(v, default):
            try:
                return int(float(str(v)))
            except Exception:
                return int(default)

        def _safe_float(v, default):
            try:
                return float(str(v))
            except Exception:
                return float(default)

        repo.upsert(
            station_id=sid,
            local_output_enabled=local_enabled,
            output_device_id=current_device,
            icecast_enabled=icecast_enabled,
            icecast_host=str(station_settings.get("icecast_host", current_host) or current_host),
            icecast_port=_safe_int(station_settings.get("icecast_port", current_port), current_port),
            icecast_mount=str(station_settings.get("icecast_mount", current_mount) or current_mount),
            icecast_user=str(
                station_settings.get("icecast_username", station_settings.get("icecast_user", current_user))
                or current_user
            ),
            icecast_password=str(station_settings.get("icecast_password", current_pass) or current_pass),
            output_gain_db=_safe_float(station_settings.get("output_gain_db", current_gain), current_gain),
            stream_codec_profile=str(
                station_settings.get("stream_codec_profile", current_profile) or current_profile
            ),
            stream_bitrate_kbps=_safe_int(
                station_settings.get("stream_bitrate_kbps", current_bitrate),
                current_bitrate,
            ),
            source_protocol=str(
                station_settings.get("source_protocol", current_protocol)
                or current_protocol
            ),
        )
        return repo.get(sid)

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        stream_album: str = "",
        track_type: str = "music",
        crossfade_seconds: float | None = None,
        start_offset_seconds: float = 0.0,
    ) -> dict:
        with self._operation_lock(station_id):
            return self._start_station_unlocked(
                station_id=station_id,
                input_uri=input_uri,
                stream_title=stream_title,
                stream_artist=stream_artist,
                stream_album=stream_album,
                track_type=track_type,
                crossfade_seconds=crossfade_seconds,
                start_offset_seconds=start_offset_seconds,
            )

    def _start_station_unlocked(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        stream_album: str = "",
        track_type: str = "music",
        crossfade_seconds: float | None = None,
        start_offset_seconds: float = 0.0,
    ) -> dict:
        init_db()
        conn = get_connection()
        repo = StationOutputRepository(conn)
        settings = SettingsRepository(conn).get_system()
        input_uri = resolve_runtime_media_path(input_uri)

        # Sync station_outputs from station_settings so output_mode
        # (speaker vs icecast) is always respected, even when the worker
        # loop starts tracks without the legacy status endpoint.
        row = self._resolve_output_settings(conn, station_id, repo)

        # Fetch station name for ffplay window title / volume mixer
        station_name = ""
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM stations WHERE id=? LIMIT 1", (int(station_id),))
            srow = cur.fetchone()
            if srow:
                station_name = str(srow["name"] or "")
        except Exception:
            pass
        station_settings = SettingsRepository(conn).get_station(int(station_id))
        if crossfade_seconds is None:
            raw_crossfade_seconds = station_settings.get(
                "default_crossfade_seconds",
                settings.get("default_crossfade_seconds", 0.0),
            )
        else:
            raw_crossfade_seconds = crossfade_seconds
        try:
            resolved_crossfade_seconds = max(0.0, float(raw_crossfade_seconds or 0.0))
        except (TypeError, ValueError):
            resolved_crossfade_seconds = 0.0
        try:
            loudness_target_lufs = float(station_settings.get("loudness_target_lufs", -16.0))
        except (TypeError, ValueError):
            loudness_target_lufs = -16.0
        system_processing_profile = str(
            settings.get("broadcast_processing_profile", "") or ""
        ).strip().lower()
        default_processing_profile = (
            system_processing_profile
            if system_processing_profile in _BROADCAST_PROCESSING_PROFILES
            else _default_processing_profile_for_station(station_name)
        )
        processing_profile = str(
            station_settings.get(
                "broadcast_processing_profile",
                default_processing_profile,
            )
            or default_processing_profile
        ).strip().lower()
        if processing_profile not in _BROADCAST_PROCESSING_PROFILES:
            processing_profile = "balanced"
        stream_features = _station_stream_feature_settings(
            station_settings,
            settings,
            station_name,
        )

        cfg = StationPipelineConfig(
            input_uri=input_uri,
            icecast_host=str(row["icecast_host"]),
            icecast_port=int(row["icecast_port"]),
            icecast_mount=str(row["icecast_mount"]),
            icecast_user=str(row["icecast_user"]),
            icecast_password=str(row["icecast_password"]),
            local_output_enabled=(
                bool(row["local_output_enabled"]) and not _local_playback_disabled()
            ),
            output_device_id=str(row["output_device_id"]),
            output_gain_db=float(row["output_gain_db"]),
            loudness_target_lufs=max(-24.0, min(-9.0, loudness_target_lufs)),
            broadcast_processing_profile=processing_profile,
            stream_codec_profile=str(row["stream_codec_profile"] or "aac_low_192"),
            stream_bitrate_kbps=int(row["stream_bitrate_kbps"] or 192),
            source_protocol=str(row["source_protocol"] or "icecast"),
            icecast_enabled=bool(row["icecast_enabled"]),
            stream_title=str(stream_title or ""),
            stream_artist=str(stream_artist or ""),
            stream_album=str(stream_album or ""),
            track_type=str(track_type or "music"),
            crossfade_seconds=resolved_crossfade_seconds,
            station_name=station_name,
            extra_icecast_outputs=_extra_icecast_outputs(settings, station_id, row),
            metadata_suppressed=(
                int(station_id) == 2
                or str(row["icecast_mount"] or "").strip().lower().startswith("/lofi")
                or "lo-fi" in station_name.lower()
            ),
            **stream_features,
        )
        # Release the per-track configuration connection before process
        # startup.  Long-running stations change tracks indefinitely; relying
        # on interpreter garbage collection here can retain SQLite handles and
        # WAL readers under sustained operation.
        conn.close()

        runtime = self._get_or_create(station_id)
        self.refresh_live_audio_settings(station_id)
        metadata_generation = self._next_metadata_generation(station_id)
        runtime.start(
            cfg,
            start_offset_seconds=max(0.0, float(start_offset_seconds or 0.0)),
        )
        with self._recovery_lock:
            self._recovery_state.pop(int(station_id), None)
        if bool(cfg.icecast_enabled):
            self._last_metadata_cfg[int(station_id)] = cfg
        else:
            self._last_metadata_cfg.pop(int(station_id), None)
        self._schedule_icecast_metadata_update(station_id, cfg, metadata_generation)
        self._required_outputs[station_id] = {
            "icecast": bool(cfg.icecast_enabled),
            "local": bool(cfg.local_output_enabled),
            **{
                f"icecast:{str(output.get('icecast_mount') or '').strip()}": True
                for output in cfg.extra_icecast_outputs
            },
        }
        return self.status(station_id)

    def stop_station(self, station_id: int) -> dict:
        with self._operation_lock(station_id):
            return self._stop_station_unlocked(station_id)

    def _stop_station_unlocked(self, station_id: int) -> dict:
        runtime = self._runtimes.get(station_id)
        if runtime:
            runtime.stop()
        with self._recovery_lock:
            self._recovery_state.pop(int(station_id), None)
        return self.status(station_id)

    def stop_all(self) -> dict:
        station_ids = [int(sid) for sid in self._runtimes.keys()]
        stopped = 0
        for station_id, runtime in list(self._runtimes.items()):
            try:
                runtime.stop()
                stopped += 1
            except Exception:
                continue
        self._runtimes.clear()
        self._required_outputs.clear()
        self._live_audio_settings_cache.clear()
        with self._recovery_lock:
            self._recovery_state.clear()
        return {
            "stations": station_ids,
            "stopped": stopped,
        }

    def status(self, station_id: int) -> dict:
        sid = int(station_id)
        runtime = self._runtimes.get(sid)
        required_outputs = self._required_outputs.get(
            sid, {"icecast": True, "local": False}
        )
        init_db()
        conn = get_connection()
        try:
            station_settings = SettingsRepository(conn).get_station(sid)
        finally:
            conn.close()
        ai_startup_status = _ai_startup_status_snapshot(sid, station_settings)
        if runtime is None:
            live_fields = self._runtime_live_fields(sid)
            return {
                "station_id": sid,
                "running": False,
                "backend": "none",
                "program_running": False,
                "output_feed_active": False,
                "transition_mode": "none",
                "transition_active": False,
                "playout_generation": 0,
                "branch_health": {"icecast": False, "local": False},
                "delivery_health": {"icecast": False, "local": False},
                "active_input_uri": "",
                "runtime_error_code": "",
                "runtime_error": "",
                "runtime_error_recoverable": True,
                "required_outputs": required_outputs,
                "recovery": self._recovery_snapshot(sid),
                "metadata_delivery": self._metadata_delivery_payload(sid),
                **ai_startup_status,
                **live_fields,
            }
        runtime_status = (
            dict(runtime.status())
            if hasattr(runtime, "status")
            else {
                "running": runtime.is_running(),
                "backend": "unknown",
                "transition_mode": "none",
                "transition_active": False,
                "branch_health": runtime.branch_health(),
            }
        )
        live_fields = self._runtime_live_fields(
            sid,
            backend=str(runtime_status.get("backend") or "none"),
        )
        runtime_status.update(
            {
                "station_id": sid,
                "required_outputs": required_outputs,
                "recovery": self._recovery_snapshot(sid),
                "metadata_delivery": self._metadata_delivery_payload(sid),
                **ai_startup_status,
            }
        )
        for key, value in live_fields.items():
            runtime_status.setdefault(key, value)
        return runtime_status

    @staticmethod
    def _recovery_error_code(exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        if any(
            token in text
            for token in ("401", "403", "unauthorized", "authentication", "password")
        ):
            return "credentials_rejected"
        if any(
            token in text
            for token in (
                "timed out",
                "refused",
                "unreachable",
                "remote end closed",
                "error number -10053",
                "error number -10054",
            )
        ):
            return "origin_unreachable"
        if any(token in text for token in ("ffmpeg", "ffplay", "gst-launch")):
            return "media_runtime_unavailable"
        return "output_recovery_failed"

    def _recovery_snapshot(self, station_id: int) -> dict:
        with self._recovery_lock:
            state = dict(self._recovery_state.get(int(station_id)) or {})
        next_attempt = float(state.pop("next_attempt_monotonic", 0.0) or 0.0)
        retry_in_seconds = max(0.0, next_attempt - time.monotonic())
        return {
            "state": str(state.get("state") or "idle"),
            "attempt_count": int(state.get("attempt_count") or 0),
            "retry_in_seconds": round(retry_in_seconds, 2),
            "error_code": str(state.get("error_code") or ""),
            "message": str(state.get("message") or ""),
        }

    def recover_station(self, station_id: int, *, force: bool = False) -> dict:
        with self._operation_lock(station_id):
            return self._recover_station_unlocked(station_id, force=force)

    def _recover_station_unlocked(
        self, station_id: int, *, force: bool = False
    ) -> dict:
        """Attempt a bounded, offset-preserving rebuild of required outputs."""
        sid = int(station_id)
        runtime = self._runtimes.get(sid)
        if runtime is None:
            return self.status(sid)

        now = time.monotonic()
        with self._recovery_lock:
            previous = dict(self._recovery_state.get(sid) or {})
            if not force and now < float(
                previous.get("next_attempt_monotonic") or 0.0
            ):
                return self.status(sid)
        if not force and self._unverified_icecast_transport_is_flowing(
            sid, runtime
        ):
            # A listener probe can briefly miss while TinyIce is publishing a
            # new AAC source. Destroying a live encoder at that point leaves a
            # stale server-side source session and turns a probe miss into a
            # real outage. Keep feeding the established transport and let the
            # mount probe converge; only dead/blocked writers are rebuilt.
            with self._recovery_lock:
                self._recovery_state[sid] = {
                    "state": "monitoring",
                    "attempt_count": int(previous.get("attempt_count") or 0),
                    "next_attempt_monotonic": (
                        time.monotonic() + _OUTPUT_MONITOR_RECHECK_SECONDS
                    ),
                    "error_code": "output_unverified",
                    "message": (
                        "The encoder and PCM writer are healthy; waiting for "
                        "listener verification without restarting the source."
                    ),
                }
            return self.status(sid)
        with self._recovery_lock:
            attempt_count = int(previous.get("attempt_count") or 0) + 1
            self._recovery_state[sid] = {
                "state": "recovering",
                "attempt_count": attempt_count,
                "next_attempt_monotonic": 0.0,
                "error_code": "",
                "message": "Rebuilding required broadcast outputs.",
            }

        try:
            runtime.recover_outputs()
            if not self.required_outputs_healthy(sid):
                raise RuntimeError("required output branch remains unhealthy")
        except Exception as exc:
            delay = _OUTPUT_RECOVERY_DELAYS_SECONDS[
                min(
                    attempt_count - 1,
                    len(_OUTPUT_RECOVERY_DELAYS_SECONDS) - 1,
                )

            ]
            error_code = self._recovery_error_code(exc)
            with self._recovery_lock:
                self._recovery_state[sid] = {
                    "state": "retry_wait",
                    "attempt_count": attempt_count,
                    "next_attempt_monotonic": time.monotonic() + delay,
                    "error_code": error_code,
                    "message": (
                        "Broadcast output recovery is waiting to retry. "
                        "Playout state has been preserved."
                    ),
                }
            _log.warning(
                "Output recovery failed station_id=%s attempt=%s code=%s "
                "retry_seconds=%.1f",
                sid,
                attempt_count,
                error_code,
                delay,
            )
            return self.status(sid)

        with self._recovery_lock:
            self._recovery_state[sid] = {
                "state": "recovered",
                "attempt_count": attempt_count,
                "next_attempt_monotonic": 0.0,
                "error_code": "",
                "message": "Required broadcast outputs recovered.",
            }
        return self.status(sid)

    def _unverified_icecast_transport_is_flowing(
        self, station_id: int, runtime
    ) -> bool:
        required = self._required_outputs.get(
            int(station_id), {"icecast": True, "local": False}
        )
        required_branches = [
            str(branch)
            for branch, enabled in required.items()
            if bool(enabled)
        ]
        if not required_branches or any(
            not branch.startswith("icecast") for branch in required_branches
        ):
            return False
        try:
            status = dict(runtime.status())
        except Exception:
            return False
        mount = status.get("icecast_mount_health")
        if not isinstance(mount, dict):
            return False
        try:
            pcm_age = float(status.get("program_pcm_age_seconds"))
            write_age = float(mount.get("last_write_age_seconds"))
        except (TypeError, ValueError):
            return False
        return bool(
            status.get("program_running")
            and not status.get("program_pcm_stalled", False)
            and pcm_age <= 2.0
            and mount.get("process_running")
            and mount.get("writer_running")
            and not mount.get("writer_failed", False)
            and not mount.get("writer_backpressured", False)
            and write_age <= 2.0
        )

    def is_process_running(self, station_id: int) -> bool:
        """Lightweight check: is the station's audio feed still active?

        Called frequently (~10x/s) by the worker loop for fast
        track-end detection. Uses only in-memory runtime state, no DB."""
        runtime = self._runtimes.get(int(station_id))
        if runtime is None:
            return False
        try:
            if hasattr(runtime, "status"):
                status = runtime.status()
                if "program_running" in status:
                    return bool(status.get("program_running"))
                if "running" in status:
                    return bool(status.get("running"))
                if "output_feed_active" in status:
                    return bool(status.get("output_feed_active"))
        except Exception:
            pass
        return bool(runtime.is_running())

    def required_outputs_healthy(self, station_id: int) -> bool:
        """Lightweight health check for outputs required by the station."""
        sid = int(station_id)
        runtime = self._runtimes.get(sid)
        if runtime is None:
            return False
        required_outputs = self._required_outputs.get(
            sid, {"icecast": True, "local": False}
        )
        try:
            branches = runtime.branch_health()
        except Exception:
            return False
        try:
            status = runtime.status() if hasattr(runtime, "status") else {}
            feed_active = bool(
                status.get("output_feed_active", False)
                or status.get("running", False)
                or runtime.is_running()
            )
        except Exception:
            feed_active = bool(runtime.is_running())
        if not feed_active:
            return False
        required_branches = [
            str(branch)
            for branch, required in required_outputs.items()
            if bool(required)
        ]
        if not required_branches:
            return True
        return all(
            bool(
                branches.get(
                    branch,
                    branches.get("icecast", False)
                    if branch.startswith("icecast:")
                    else False,
                )
            )
            for branch in required_branches
        )

    def snapshot(self) -> list[dict]:
        station_ids = sorted(
            {
                int(sid)
                for sid in (
                    list(self._runtimes.keys()) + list(self._required_outputs.keys())
                )
            }
        )
        return [self.status(station_id) for station_id in station_ids]
