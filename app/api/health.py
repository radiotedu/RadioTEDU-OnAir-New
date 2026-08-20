import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.runtime import runtime_registry, worker_loop_manager
from app.config import get_db_path
from app.dependency_bootstrap import managed_binary_path, read_bootstrap_state
from app.db import database_health_snapshot, get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_repo import StationRepository
from app.runtime_identity import (
    BACKEND_INSTANCE_ID,
    BACKEND_PROCESS_ID,
    BACKEND_STARTED_AT_EPOCH,
)
from app.runtime_paths import resolve_binary_details
from app.version import PRODUCT_VERSION

router = APIRouter()
logger = logging.getLogger("cleanroom.health")


@router.get("/api/health/live")
def liveness():
    return {
        "status": "ok",
        "state": "operational",
        "service": "radiotedu-onair",
        "version": PRODUCT_VERSION,
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "backend_process_id": BACKEND_PROCESS_ID,
    }


@router.get("/api/health/ready")
def readiness():
    try:
        init_db()
        database = database_health_snapshot(force=True)
    except Exception as exc:
        logger.warning("Readiness database check failed code=%s", type(exc).__name__)
        database = {
            "state": "critical",
            "healthy": False,
            "integrity": "unavailable",
        }
    from app.services.ha_coordinator import ha_coordinator
    ha = ha_coordinator.snapshot()
    ready = bool(database.get("healthy"))
    payload = {
        "status": "ok" if ready else "unavailable",
        "state": "operational" if ready else "critical",
        "ready": ready,
        "database": "ok" if bool(database.get("healthy")) else "unavailable",
        "broadcast_safe": bool(ha.get("safe_to_broadcast")),
        "service": "radiotedu-onair",
        "version": PRODUCT_VERSION,
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "backend_process_id": BACKEND_PROCESS_ID,
    }
    return JSONResponse(payload, status_code=200 if ready else 503)


def describe_dependency(*names: str) -> dict[str, str | bool]:
    state = read_bootstrap_state()
    managed_path = str(managed_binary_path(names[0]))
    state_entry = next((state.get(name) for name in names if state.get(name)), {})

    for name in names:
        details = resolve_binary_details(name)
        if bool(details.get("found")):
            return {
                "found": True,
                "path": str(details.get("path") or ""),
                "source": str(details.get("source") or ""),
                "managed_path": managed_path,
                "bootstrap_status": str((state_entry or {}).get("status") or ""),
                "bootstrap_error": str((state_entry or {}).get("error") or ""),
            }

    return {
        "found": False,
        "path": "",
        "source": "",
        "managed_path": managed_path,
        "bootstrap_status": str((state_entry or {}).get("status") or ""),
        "bootstrap_error": str((state_entry or {}).get("error") or ""),
    }


def _cached_setup_dependency_state() -> dict[str, dict]:
    """Report the last bootstrap result without doing installer work on health polls."""
    state = read_bootstrap_state()
    return {
        "webview2": dict(state.get("webview2-runtime") or {}),
        "ollama": dict(state.get("ollama.exe") or {}),
        "python_runtime": dict(state.get("python-runtime") or {}),
        "qwen_tts_runtime": dict(state.get("qwen-tts-runtime") or {}),
    }


def _resolve_station_snapshot(conn, requested_station_id: int | None) -> tuple[int, str, int]:
    repo = StationRepository(conn)
    rows = list(repo.list_all())
    if not rows:
        created_id = repo.create("Main Radio")
        repo.set_active(created_id)
        rows = list(repo.list_all())

    active = repo.get_active()
    active_station_id = int(active["id"]) if active else int(rows[0]["id"])
    sid = int(requested_station_id) if requested_station_id is not None else active_station_id
    station_row = next((row for row in rows if int(row["id"]) == sid), None)
    if station_row is None:
        station_row = next(
            (row for row in rows if int(row["id"]) == active_station_id),
            rows[0],
        )
        sid = int(station_row["id"])
    station_name = str(station_row["name"] or f"Station {sid}")
    return sid, station_name, active_station_id


@router.get("/api/health")
def health(station_id: int | None = None):
    init_db()
    database = database_health_snapshot()
    conn = get_connection()
    sid, station_name, active_station_id = _resolve_station_snapshot(conn, station_id)
    runtime_status = runtime_registry.status(sid)
    worker_loop_status = worker_loop_manager.status(sid)
    engine_running = bool(runtime_status.get("running"))
    settings = SettingsRepository(conn).get_station(sid)
    ai_enabled = str(settings.get("ai_host_enabled", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        if not ai_enabled:
            ai_prefetch_stats = {"state": "disabled"}
            ai_prefetch_readiness = {"state": "disabled"}
        else:
            from app.services.ai_prefetch import get_ai_prefetch

            ai_prefetch = get_ai_prefetch()
            ai_prefetch_stats = ai_prefetch.get_stats(sid)
            ai_prefetch_readiness = ai_prefetch.readiness_snapshot(sid)
    except Exception:
        ai_prefetch_stats = {}
        ai_prefetch_readiness = {}

    dependency_state = _cached_setup_dependency_state()
    try:
        from app.api.setup import _ai_status, _startup_ai_readiness

        ai_runtime_status = _ai_status(settings)
        startup_ai_state = _startup_ai_readiness(sid, settings)
    except Exception:
        ai_runtime_status = {}
        startup_ai_state = {}
    output_mode = str(settings.get("output_mode", "speaker") or "speaker")
    speaker_monitor_enabled = (
        str(settings.get("speaker_monitor_enabled", "true")).strip().lower()
        not in {"0", "false", "off", "no"}
    )

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM tracks WHERE station_id=? AND is_active=1", (sid,))
    tracks_in_library = int(cur.fetchone()["c"])

    data_root = get_db_path().parent
    from app.audio.guest_audio_registry import guest_audio_registry
    from app.services.audit_chain import audit_chain
    from app.services.ha_coordinator import ha_coordinator

    public_runtime_status = dict(runtime_status)
    public_runtime_status.pop("active_input_uri", None)
    public_runtime_registry = []
    for item in runtime_registry.snapshot():
        public_item = dict(item)
        public_item.pop("active_input_uri", None)
        public_runtime_registry.append(public_item)

    payload = {
        "status": "ok" if bool(database.get("healthy")) else "degraded",
        "overall_state": str(database.get("state") or "unknown"),
        "database": database,
        "backend_instance_id": BACKEND_INSTANCE_ID,
        "backend_process_id": BACKEND_PROCESS_ID,
        "backend_started_at_epoch": BACKEND_STARTED_AT_EPOCH,
        "station_id": sid,
        "station_name": station_name,
        "active_station_id": active_station_id,
        "output_mode": output_mode,
        "speaker_monitor_enabled": speaker_monitor_enabled,
        "tracks_in_library": tracks_in_library,
        "engine_running": engine_running,
        "runtime_branch_health": runtime_status.get("branch_health", {}),
        "runtime_delivery_health": runtime_status.get("delivery_health", {}),
        "runtime": public_runtime_status,
        "worker_loop": worker_loop_status,
        "ai_prefetch": {
            "stats": ai_prefetch_stats,
            "readiness": ai_prefetch_readiness,
            "startup_state": startup_ai_state,
            "runtime": ai_runtime_status,
        },
        "setup_dependencies": dependency_state,
        "runtime_registry": public_runtime_registry,
        "worker_loops": worker_loop_manager.snapshot(),
        "high_availability": ha_coordinator.snapshot(),
        "audit_chain": audit_chain.verify(),
        "guest_audio": guest_audio_registry.snapshots(sid),
        "dependencies": {
            "ffmpeg": describe_dependency("ffmpeg.exe", "ffmpeg"),
            "ffplay": describe_dependency("ffplay.exe", "ffplay"),
            "ffprobe": describe_dependency("ffprobe.exe", "ffprobe"),
            "gst_launch": describe_dependency("gst-launch-1.0.exe", "gst-launch-1.0"),
            "yt_dlp": describe_dependency("yt-dlp.exe", "yt-dlp"),
        },
        "liquidsoap_connected": False,
        "music_dir": str(data_root / "media" / "music"),
        "jingle_dir": str(data_root / "media" / "jingles"),
        "ads_dir": str(data_root / "media" / "ads"),
    }
    conn.close()
    return payload
