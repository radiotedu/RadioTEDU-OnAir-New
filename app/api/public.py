from datetime import datetime, timezone
from contextlib import closing
import threading
import time
from fastapi import APIRouter

from app.api.runtime import runtime_registry, worker_loop_manager
from app.config import get_public_base_url
from app.db import get_connection, init_db
from app.repositories.queue_repo import QueueRepository
from app.repositories.settings_repo import SettingsRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.repositories.station_repo import StationRepository
from app.services.audio_stream_probe import (
    configured_listener_url,
    probe_audio_url,
)

router = APIRouter()
_ORIGIN_PROBE_TTL_SECONDS = 5.0
_ORIGIN_PROBE_TIMEOUT_SECONDS = 1.5
_ORIGIN_SUCCESS_THRESHOLD = 2
_ORIGIN_FAILURE_THRESHOLD = 2
_origin_probe_cache: dict[tuple, dict] = {}
_origin_probe_lock = threading.Lock()


def _normalized_mapping(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _public_status_summary(
    runtime_state: dict,
    worker_state: dict,
    *,
    icecast_origin_confirmed: bool | None = None,
) -> tuple[str, str]:
    runtime_running = bool(runtime_state.get("running"))
    program_running = bool(runtime_state.get("program_running"))
    program_pcm_stalled = bool(runtime_state.get("program_pcm_stalled"))
    active_input_uri = str(runtime_state.get("active_input_uri") or "").strip().lower()
    worker_running = bool(worker_state.get("running"))
    branch_health = _normalized_mapping(runtime_state.get("branch_health"))
    delivery_health = _normalized_mapping(runtime_state.get("delivery_health"))
    verified_health = delivery_health or branch_health
    required_outputs = _normalized_mapping(runtime_state.get("required_outputs"))

    required_keys = [
        str(key)
        for key, required in required_outputs.items()
        if bool(required)
    ]
    healthy_required = [
        key for key in required_keys if bool(verified_health.get(key))
    ]

    if not runtime_running and not program_running and not worker_running:
        return "offline", "Runtime and worker are stopped"

    if program_running and program_pcm_stalled:
        return (
            "degraded",
            "Playout process is running but program audio stopped advancing",
        )

    if runtime_running and active_input_uri.startswith("silence://"):
        return (
            "degraded",
            "Continuity fallback is active; no program audio is playing",
        )

    all_required_healthy = not required_keys or len(healthy_required) == len(required_keys)
    if (
        "icecast" in required_keys
        and icecast_origin_confirmed is False
        and (runtime_running or program_running)
    ):
        return (
            "degraded",
            "Playout is running but the public mount did not deliver audio bytes",
        )
    if runtime_running and all_required_healthy:
        if worker_running and not str(worker_state.get("last_error") or "").strip():
            return "live", "Runtime healthy"
        if worker_running:
            return "degraded", "Worker reported an issue"
        return "degraded", "Runtime is running but worker is inactive"

    if runtime_running:
        return "degraded", "Runtime is running but required outputs are degraded"

    return "degraded", "Worker is running without an active runtime"


def _probe_icecast_origin(
    station_id: int,
    output,
    station_settings: dict,
    public_base_url: str = "",
) -> bool | None:
    if output is None or not bool(output["icecast_enabled"]):
        return None
    probe_url = configured_listener_url(
        output,
        station_settings,
        public_base_url,
    )
    if not probe_url:
        return None
    key = (int(station_id), probe_url.lower())
    now = time.monotonic()
    with _origin_probe_lock:
        cached = dict(_origin_probe_cache.get(key, {}))
        if cached and now - float(cached.get("checked_at", 0.0)) < _ORIGIN_PROBE_TTL_SECONDS:
            return bool(cached.get("confirmed"))

    ok = probe_audio_url(
        probe_url,
        timeout=_ORIGIN_PROBE_TIMEOUT_SECONDS,
    ).ok

    with _origin_probe_lock:
        previous = dict(_origin_probe_cache.get(key, {}))
        previous_confirmed = bool(previous.get("confirmed"))
        if ok:
            successes = int(previous.get("successes", 0)) + 1
            failures = 0
            confirmed = bool(
                previous_confirmed
                or successes >= _ORIGIN_SUCCESS_THRESHOLD
            )
        else:
            successes = 0
            failures = int(previous.get("failures", 0)) + 1
            confirmed = bool(
                previous_confirmed
                and failures < _ORIGIN_FAILURE_THRESHOLD
            )
        _origin_probe_cache[key] = {
            "checked_at": now,
            "confirmed": confirmed,
            "successes": successes,
            "failures": failures,
        }
    return confirmed


def _public_now_playing(conn, station_id: int) -> dict | None:
    current = QueueRepository(conn).current_playing(station_id)
    if current is None:
        return None
    started_at = _public_started_at_iso(current["started_at"])
    return {
        "title": str(current["title"] or ""),
        "artist": str(current["artist"] or ""),
        "track_type": str(current["track_type"] or "music"),
        "duration": float(current["duration"] or 0.0),
        "started_at": started_at,
    }


def _public_started_at_iso(value) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T")
    try:
        started_at = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_active_show_name(conn, station_id: int) -> str | None:
    session = ShowSessionRepository(conn).get_active_for_station(station_id)
    if not session:
        return None
    show = ShowRepository(conn).get(int(session["show_id"]))
    if not show:
        return None
    return str(show.get("name") or "") or None


@router.get("/api/public/stations")
def list_public_station_summaries():
    init_db()
    with closing(get_connection()) as conn:
        stations = []
        station_outputs = StationOutputRepository(conn)
        settings = SettingsRepository(conn)
        system_settings = settings.get_system()
        public_base_url = str(
            get_public_base_url() or system_settings.get("stream_public_base_url") or ""
        ).strip()
        for station in StationRepository(conn).list_all():
            station_id = int(station["id"])
            try:
                runtime_state = dict(runtime_registry.status(station_id))
            except Exception:
                runtime_state = {}
            try:
                worker_state = dict(worker_loop_manager.status(station_id))
            except Exception:
                worker_state = {}

            origin_confirmed = None
            required_outputs = _normalized_mapping(
                runtime_state.get("required_outputs")
            )
            if (
                (
                    bool(runtime_state.get("running"))
                    or bool(runtime_state.get("program_running"))
                )
                and bool(required_outputs.get("icecast"))
            ):
                origin_confirmed = _probe_icecast_origin(
                    station_id,
                    station_outputs.get_raw(station_id),
                    settings.get_station(station_id),
                    public_base_url,
                )
            status, status_reason = _public_status_summary(
                runtime_state,
                worker_state,
                icecast_origin_confirmed=origin_confirmed,
            )
            reported_item = _public_now_playing(conn, station_id)
            stations.append(
                {
                    "id": station_id,
                    "name": str(station["name"] or ""),
                    "status": status,
                    "status_reason": status_reason,
                    "now_playing": reported_item if status == "live" else None,
                    "preserved_item": reported_item if status != "live" else None,
                    "active_show_name": _public_active_show_name(conn, station_id),
                }
            )
        return {"stations": stations}
