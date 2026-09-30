"""Sanitized, cached, loopback-only telemetry for a passwordless local Health Wall."""
from __future__ import annotations

import threading
import time
import math
import json
from copy import deepcopy
from datetime import date, datetime, time as day_time, timedelta, timezone
from fastapi import APIRouter, HTTPException, Request
from zoneinfo import ZoneInfo

from app.api.public import (
    _public_active_show_name,
    _public_now_playing,
    list_public_station_summaries,
)
from app.api.runtime import _runtime_status_payload
from app.audio.live_mic_registry import live_mic_registry
from app.audio.microphone_readiness import physical_microphone_readiness
from app.db import get_connection, init_db
from app.repositories.station_repo import StationRepository
from app.services.managed_library_watcher import get_managed_library_watcher
from app.services.product_media_catalog import get_product_media_catalog_service
from app.services.public_stream_evidence import get_public_stream_evidence_service
from app.services.radiotedu_service_control import (
    SETTINGS_KEY as SERVICE_CONTROL_SETTINGS_KEY,
    all_service_statuses,
    load_settings as load_service_control_settings,
)
from app.repositories.settings_repo import SettingsRepository
from app.services.unified_media_folder import get_unified_media_folder_service

router = APIRouter(tags=["health-wall"])
_FAST_CACHE_TTL_SECONDS = 2.0
_SLOW_CACHE_TTL_SECONDS = 30.0
_cache_lock = threading.Lock()
_fast_cache: tuple[float, dict] | None = None
_slow_cache: tuple[float, dict] | None = None
_service_cache_lock = threading.Lock()
_service_cache: tuple[float, dict] | None = None
_service_refreshing = False

def _loopback_only(request: Request) -> None:
    host = (request.client.host if request.client else "").strip()
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="health_wall_loopback_only")

def _safe_worker_loop(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    return {
        key: source.get(key)
        for key in (
            "running",
            "stopping",
            "interval_sec",
            "ticks",
            "failure_count",
            "last_backoff_seconds",
            "next_attempt_in_seconds",
        )
    }


def _safe_runtime(value: dict) -> dict:
    result = {
        key: value.get(key)
        for key in (
            "state",
            "alive",
            "running",
            "program_running",
            "output_feed_active",
            "program_pcm_stalled",
            "producer_eof",
            "liquidsoap_connected",
            "output_mode",
            "active_stream_title",
            "active_stream_artist",
            "active_track_type",
        )
        if key in value
    }
    result["worker_loop"] = _safe_worker_loop(value.get("worker_loop"))
    branches = value.get("branch_health")
    if isinstance(branches, dict):
        result["branches"] = {
            str(name)[:80]: bool(healthy)
            for name, healthy in branches.items()
            if isinstance(name, str)
        }
    deliveries = value.get("delivery_health")
    if isinstance(deliveries, dict):
        result["deliveries"] = {
            str(name)[:80]: bool(healthy)
            for name, healthy in deliveries.items()
            if isinstance(name, str)
        }
    mount = value.get("icecast_mount_health")
    if isinstance(mount, dict):
        result["icecast_mount_health"] = {
            key: mount.get(key)
            for key in (
                "process_running",
                "mount_healthy",
                "consecutive_probe_failures",
                "last_network_write_age_seconds",
                "encoded_bytes_sent",
            )
            if key in mount
        }
    return result


def _monitor_playout_snapshot(conn, station_id: int) -> dict:
    """Return small, path-free program readiness details for the local monitor."""
    try:
        next_item = conn.execute(
            "SELECT COALESCE(t.title,'') AS title, COALESCE(t.artist,'') AS artist, "
            "COALESCE(t.track_type,'music') AS track_type "
            "FROM queue_items q JOIN tracks t ON t.id=q.track_id "
            "WHERE q.station_id=? AND t.station_id=? AND q.status='pending' "
            "AND t.is_active=1 AND COALESCE(t.file_path,'')<>'' "
            "AND (q.retry_after IS NULL OR datetime(q.retry_after)<=CURRENT_TIMESTAMP) "
            "ORDER BY q.position, q.id LIMIT 1",
            (int(station_id), int(station_id)),
        ).fetchone()
        inventory = conn.execute(
            "SELECT "
            "SUM(CASE WHEN is_active=1 AND COALESCE(file_path,'')<>'' "
            "AND LOWER(COALESCE(track_type,'music'))='music' "
            "AND COALESCE(exclude_from_autoplay,0)=0 THEN 1 ELSE 0 END) AS eligible, "
            "(SELECT COUNT(*) FROM queue_items q JOIN tracks qt ON qt.id=q.track_id "
            " WHERE q.station_id=? AND qt.station_id=? AND q.status='pending' "
            " AND qt.is_active=1 AND COALESCE(qt.file_path,'')<>'' "
            " AND (q.retry_after IS NULL OR datetime(q.retry_after)<=CURRENT_TIMESTAMP)) AS pending "
            "FROM tracks WHERE station_id=?",
            (int(station_id), int(station_id), int(station_id)),
        ).fetchone()
        eligible = int((inventory["eligible"] if inventory else 0) or 0)
        pending = int((inventory["pending"] if inventory else 0) or 0)
        return {
            "next_title": str(next_item["title"] or "") if next_item else "",
            "next_artist": str(next_item["artist"] or "") if next_item else "",
            "next_track_type": str(next_item["track_type"] or "music") if next_item else "",
            "eligible_music_count": eligible,
            "pending_count": pending,
            "program_ready": eligible > 0 or pending > 0,
        }
    except Exception:
        return {
            "next_title": "",
            "next_artist": "",
            "next_track_type": "",
            "eligible_music_count": 0,
            "pending_count": 0,
            "program_ready": False,
            "state": "unavailable",
        }


def _monitor_plan_window_active(plan, now: datetime | None = None) -> bool:
    zone = ZoneInfo(str(plan["timezone"] or "Europe/Istanbul"))
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    starts_on = date.fromisoformat(str(plan["starts_on"]))
    ends_on = date.fromisoformat(str(plan["ends_on"]))
    start_time = day_time.fromisoformat(str(plan["local_start"]))
    end_time = day_time.fromisoformat(str(plan["local_end"]))
    weekdays = {int(value) for value in json.loads(str(plan["weekdays_json"] or "[]"))}
    for anchor_day in (current.date(), current.date() - timedelta(days=1)):
        if not starts_on <= anchor_day <= ends_on or anchor_day.isoweekday() not in weekdays:
            continue
        anchor_start = datetime.combine(anchor_day, start_time, tzinfo=zone)
        anchor_end = datetime.combine(anchor_day, end_time, tzinfo=zone)
        if anchor_end <= anchor_start:
            anchor_end += timedelta(days=1)
        if anchor_start <= current < anchor_end:
            return True
    return False


def _monitor_ad_snapshot(conn, station_id: int) -> dict:
    """Expose the real RadioTEDU ad queue/cadence state without local DB access."""
    try:
        playing = conn.execute(
            "SELECT a.id, a.status, a.due_at, COALESCE(t.title,'') AS title, "
            "COALESCE(t.artist,'') AS artist, "
            "CASE WHEN COALESCE(t.file_path,'')<>'' THEN 1 ELSE 0 END AS media_ready "
            "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
            "WHERE a.station_id=? AND a.status='playing' "
            "ORDER BY a.started_at, a.id LIMIT 1",
            (int(station_id),),
        ).fetchone()
        if playing:
            return {"state": "playing", **dict(playing)}

        pending = conn.execute(
            "SELECT a.id, a.status, a.due_at, COALESCE(t.title,'') AS title, "
            "COALESCE(t.artist,'') AS artist, "
            "CASE WHEN COALESCE(t.file_path,'')<>'' THEN 1 ELSE 0 END AS media_ready "
            "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
            "WHERE a.station_id=? AND a.status='pending' "
            "ORDER BY datetime(a.due_at), a.priority DESC, a.id LIMIT 1",
            (int(station_id),),
        ).fetchone()
        plans = conn.execute(
            "SELECT p.id, p.name, p.enabled, p.starts_on, p.ends_on, "
            "p.weekdays_json, p.local_start, p.local_end, p.timezone, "
            "p.repeat_every_songs, p.priority, p.created_at, "
            "t.track_id AS target_track_id, t.enabled AS target_enabled, "
            "COALESCE(tr.is_active,0) AS track_active, "
            "CASE WHEN COALESCE(tr.file_path,'')<>'' THEN 1 ELSE 0 END AS media_ready "
            "FROM broadcast_plans p "
            "LEFT JOIN broadcast_plan_targets t ON t.plan_id=p.id AND t.station_id=? "
            "LEFT JOIN tracks tr ON tr.id=t.track_id "
            "WHERE p.plan_type='ad' AND p.cadence_mode='songs' "
            "AND EXISTS (SELECT 1 FROM broadcast_plan_targets target "
            "            WHERE target.plan_id=p.id AND target.station_id=?) "
            "ORDER BY p.priority DESC, p.id DESC",
            (int(station_id), int(station_id)),
        ).fetchall()
        active = []
        inactive_reason = ""
        for plan in plans:
            if not bool(plan["enabled"]):
                inactive_reason = "plan_disabled"
                continue
            if not bool(plan["target_enabled"]):
                inactive_reason = "target_disabled"
                continue
            try:
                if not _monitor_plan_window_active(plan):
                    inactive_reason = "outside_schedule_window"
                    continue
            except Exception as exc:
                inactive_reason = f"invalid_plan:{type(exc).__name__}"
                continue
            if not bool(plan["track_active"]) or not bool(plan["media_ready"]):
                inactive_reason = "ad_track_inactive_or_missing"
                continue
            active.append(plan)

        if pending:
            payload = {key: value for key, value in dict(pending).items() if key != "media_ready"}
            payload["state"] = "pending" if bool(pending["media_ready"]) else "pending_media_missing"
            return payload
        if not active:
            if plans:
                return {"state": "inactive", "reason": inactive_reason or "no_eligible_target"}
            return {"state": "no_plan"}

        plan = active[0]
        interval = max(1, int(plan["repeat_every_songs"] or 10))
        count_row = conn.execute(
            "SELECT COUNT(*) AS amount FROM queue_items q JOIN tracks t ON t.id=q.track_id "
            "WHERE q.station_id=? AND t.station_id=? AND q.status='done' "
            "AND LOWER(COALESCE(t.track_type,'music'))='music' "
            "AND q.finished_at IS NOT NULL AND datetime(q.finished_at)>=datetime(?)",
            (int(station_id), int(station_id), str(plan["created_at"] or "1970-01-01 00:00:00")),
        ).fetchone()
        music_count = int((count_row["amount"] if count_row else 0) or 0)
        completed_cycles = music_count // interval
        statuses = conn.execute(
            "SELECT dedupe_key, status FROM ad_break_items WHERE station_id=? "
            "AND dedupe_key LIKE ? ORDER BY id DESC",
            (int(station_id), f"broadcast-plan:{int(plan['id'])}:song:{int(station_id)}:%"),
        ).fetchall()
        status_by_cycle: dict[int, str] = {}
        for row in statuses:
            try:
                cycle = int(str(row["dedupe_key"]).rsplit(":", 1)[1])
            except (IndexError, TypeError, ValueError):
                continue
            status_by_cycle.setdefault(cycle, str(row["status"] or ""))
        for cycle in range(1, completed_cycles + 1):
            if status_by_cycle.get(cycle) != "done":
                return {
                    "state": "unmaterialized",
                    "reason": "due_without_pending_row",
                    "name": str(plan["name"] or ""),
                    "interval": interval,
                    "remaining": 0,
                    "cycle": cycle,
                }
        remainder = music_count % interval
        remaining = interval - remainder if remainder else interval
        return {
            "state": "countdown",
            "name": str(plan["name"] or ""),
            "interval": interval,
            "remaining": remaining,
        }
    except Exception:
        return {"state": "unavailable"}


def _runtime_health(runtime: dict) -> str:
    process_active = bool(
        runtime.get("alive") or runtime.get("running") or runtime.get("program_running")
    )
    if runtime.get("liquidsoap_connected") is False and process_active:
        return "degraded"
    deliveries = runtime.get("delivery_health")
    branches = runtime.get("branch_health")
    health_map = deliveries if isinstance(deliveries, dict) and deliveries else branches
    required = runtime.get("required_outputs")
    if isinstance(health_map, dict) and process_active:
        names = (
            [name for name, enabled in required.items() if bool(enabled)]
            if isinstance(required, dict) and required
            else list(health_map)
        )
        for name in names:
            value = health_map.get(name)
            if value is False:
                return "degraded"
            if isinstance(value, dict) and any(
                value.get(key) is False for key in ("healthy", "connected", "ready")
            ):
                return "degraded"
    if process_active:
        return "healthy"
    if _safe_worker_loop(runtime.get("worker_loop")).get("running"):
        return "degraded"
    return "unavailable"


def _safe_microphone(station_id: int) -> dict:
    try:
        value = live_mic_registry.snapshot(station_id)
        if not isinstance(value, dict):
            raise TypeError("invalid_microphone_snapshot")

        def finite_metric(key: str, fallback: float) -> float:
            try:
                metric = float(value.get(key, fallback))
            except (TypeError, ValueError, OverflowError):
                return fallback
            return metric if math.isfinite(metric) else fallback

        transmitting = bool(value.get("transmitting"))
        receiving = bool(value.get("receiving"))
        enabled = bool(value.get("live_input_enabled"))
        level_db = finite_metric("level_db", -60.0)
        peak_db = finite_metric("peak_db", -60.0)
        try:
            buffer_bytes = max(0, int(value.get("buffer_bytes") or 0))
        except (TypeError, ValueError, OverflowError):
            buffer_bytes = 0
        if transmitting and receiving:
            state = "healthy"
        elif enabled or transmitting:
            state = "degraded"
        else:
            state = "idle"
        physical = physical_microphone_readiness.snapshot(
            live=transmitting,
            receiving=receiving,
        )
        physical["label"] = ""
        if state == "healthy" and physical.get("selection") == "not-present":
            state = "degraded"
        if state == "healthy" and bool(physical.get("stale")):
            state = "degraded"
        return {
            "state": state,
            "capability": True,
            "transmitting": transmitting,
            "receiving": receiving,
            "transport": str(value.get("transport") or "unknown")[:20],
            "source_name": "",  # Source labels may contain local paths or personal device names.
            "level_db": level_db,
            "peak_db": peak_db,
            "buffer_bytes": buffer_bytes,
            "silent": bool(receiving and level_db <= -55.0),
            "clipping": bool(receiving and peak_db >= -0.5),
            "has_error": bool(value.get("last_error")),
            "physical_device": physical,
        }
    except Exception:
        return {
            "state": "unknown",
            "capability": False,
            "physical_device": physical_microphone_readiness.snapshot(
                live=False,
                receiving=False,
            ),
        }


def _safe_snapshot(factory, sanitizer):
    try:
        return {"state": "healthy", **sanitizer(factory())}
    except Exception:
        return {"state": "unavailable"}

def _collect_fast() -> dict:
    init_db()
    try:
        public_by_id = {
            int(item["id"]): item
            for item in list_public_station_summaries().get("stations", [])
        }
    except Exception:
        public_by_id = {}
    conn = get_connection()
    try:
        stations = []
        for row in StationRepository(conn).list_all():
            station_id = int(row["id"])
            runtime = dict(_runtime_status_payload(station_id))
            public_station = public_by_id.get(station_id)
            public_status = str((public_station or {}).get("status") or "offline")
            reported_item = _public_now_playing(conn, station_id)
            stations.append({
                "station_id": station_id,
                "name": str(row["name"]),
                "health": {
                    "live": "healthy",
                    "degraded": "degraded",
                    "offline": "unavailable",
                }.get(public_status, _runtime_health(runtime)),
                "now_playing": (public_station or {}).get("now_playing"),
                "preserved_item": (
                    (public_station or {}).get("preserved_item")
                    if public_station is not None
                    else reported_item
                ),
                "active_show_name": _public_active_show_name(conn, station_id),
                "runtime": _safe_runtime(runtime),
                "playout_monitor": _monitor_playout_snapshot(conn, station_id),
                "microphones": _safe_microphone(station_id),
            })
        ad_monitor = _monitor_ad_snapshot(conn, 4)
    finally:
        conn.close()
    return {
        "observed_at": int(time.time()),
        "stations": stations,
        "ad_monitor": ad_monitor,
    }


def _safe_watcher(value: dict) -> dict:
    profiles = []
    for item in value.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        profiles.append(
            {
                key: item.get(key)
                for key in (
                    "station_id",
                    "track_type",
                    "mode",
                    "status",
                    "last_scan_at",
                    "last_sync_at",
                    "retry_count",
                )
            }
        )
    return {"running": bool(value.get("running")), "profiles": profiles}


def _safe_unified_media(value: dict) -> dict:
    return {
        "layout_ready": bool(value.get("layout_ready")),
        "last_published_at": str(value.get("last_published_at") or ""),
        "last_refresh_at": str(value.get("last_refresh_at") or ""),
        "has_error": bool(value.get("last_error")),
        "views": [
            {
                key: item.get(key)
                for key in (
                    "view",
                    "exists",
                    "file_count",
                    "generated_count",
                    "operator_count",
                )
            }
            for item in value.get("views") or []
            if isinstance(item, dict)
        ],
    }


def _safe_product_catalog(value: dict) -> dict:
    products = []
    for item in value.get("products") or []:
        if not isinstance(item, dict):
            continue
        products.append(
            {
                key: item.get(key)
                for key in (
                    "product",
                    "state",
                    "file_count",
                    "generation",
                    "last_good_generation",
                    "last_scan_at",
                    "last_sync_at",
                    "retry_count",
                    "error_code",
                )
            }
        )
    return {
        "running": bool(value.get("running")),
        "poll_interval_seconds": value.get("poll_interval_seconds"),
        "products": products,
    }


def _safe_services(value: list[dict]) -> list[dict]:
    services = []
    for item in value:
        if not isinstance(item, dict):
            continue
        checks = item.get("health") if isinstance(item.get("health"), list) else []
        autonomous = item.get("autonomous_startup")
        if not isinstance(autonomous, dict):
            autonomous = {}
        database = item.get("database")
        if not isinstance(database, dict):
            database = {}
        services.append(
            {
                "id": str(item.get("id") or "")[:80],
                "product": str(item.get("product") or "")[:120],
                "name": str(item.get("name") or "")[:120],
                "enabled": bool(item.get("enabled")),
                "auto_start": bool(item.get("auto_start")),
                "state": str(item.get("state") or "unknown")[:40],
                "runtime": str(item.get("runtime") or "unknown")[:40],
                "config_ready": bool(item.get("config_ready")),
                "startup_owner": str(item.get("startup_owner") or "")[:80],
                "health_checks": len(checks),
                "health_checks_ok": sum(1 for check in checks if isinstance(check, dict) and check.get("ok")),
                "autonomous_startup": {
                    key: autonomous.get(key)
                    for key in ("state", "ready", "supported", "verified_at")
                    if key in autonomous
                },
                "database": {
                    key: database.get(key)
                    for key in ("state", "ready", "supported", "kind")
                    if key in database
                },
            }
        )
    return services


def _integration_state(services: list[dict], *service_ids: str) -> str:
    matching = [item for item in services if item.get("id") in service_ids]
    enabled = [item for item in matching if item.get("enabled")]
    if not enabled:
        return "disabled"
    states = {str(item.get("state") or "unknown") for item in enabled}
    if states == {"healthy"}:
        return "healthy"
    if states & {"degraded", "ready"}:
        return "degraded"
    if states & {"not_ready"}:
        return "unavailable"
    return "unknown"


def _refresh_service_health(raw_settings: str | None) -> None:
    global _service_cache, _service_refreshing
    snapshot = _safe_snapshot(
        lambda: all_service_statuses(
            load_service_control_settings(raw_settings), include_health=True
        ),
        lambda value: {"items": _safe_services(value)},
    )
    snapshot["observed_at"] = int(time.time())
    with _service_cache_lock:
        _service_cache = (time.monotonic(), snapshot)
        _service_refreshing = False


def _service_snapshot(raw_settings: str | None) -> dict:
    global _service_refreshing
    now = time.monotonic()
    with _service_cache_lock:
        cached = deepcopy(_service_cache[1]) if _service_cache else None
        stale = not _service_cache or now - _service_cache[0] >= _SLOW_CACHE_TTL_SECONDS
        if stale and not _service_refreshing:
            _service_refreshing = True
            threading.Thread(
                target=_refresh_service_health,
                args=(raw_settings,),
                name="health-wall-service-probe",
                daemon=True,
            ).start()
    if cached is not None:
        return cached
    initial = _safe_snapshot(
        lambda: all_service_statuses(
            load_service_control_settings(raw_settings), include_health=False
        ),
        lambda value: {"items": _safe_services(value)},
    )
    initial["state"] = "probing" if "items" in initial else "unavailable"
    initial["observed_at"] = int(time.time())
    return initial


def _collect_slow() -> dict:
    init_db()
    conn = get_connection()
    try:
        system_settings = SettingsRepository(conn).get_system()
        raw_settings = system_settings.get(SERVICE_CONTROL_SETTINGS_KEY)
    finally:
        conn.close()
    services = _service_snapshot(raw_settings)
    public_streams = get_public_stream_evidence_service().snapshot(system_settings)
    service_items = services.get("items") if isinstance(services.get("items"), list) else []
    return {
        "observed_at": int(time.time()),
        "library": {
            "watcher": _safe_snapshot(
                lambda: get_managed_library_watcher().snapshot(), _safe_watcher
            ),
            "unified_media": _safe_snapshot(
                lambda: get_unified_media_folder_service().status(), _safe_unified_media
            ),
            "product_catalog": _safe_snapshot(
                lambda: get_product_media_catalog_service().snapshot(),
                _safe_product_catalog,
            ),
        },
        "integrations": {
            "juke": _integration_state(
                service_items, "juke_media_agent", "juke_backend"
            ),
            "voting": _integration_state(
                service_items, "voting_agent", "voting_backend"
            ),
            "icecast": "unknown",
            "public_ai": str(
                (public_streams.get("streams", {}).get("ai", {}) or {}).get("state")
                or "unknown"
            ),
            "public_event": str(
                (public_streams.get("streams", {}).get("event", {}) or {}).get("state")
                or "unknown"
            ),
        },
        "public_stream_evidence": public_streams,
        "services": services,
    }


def _cached_snapshot() -> dict:
    global _fast_cache, _slow_cache
    now = time.monotonic()
    with _cache_lock:
        if not _fast_cache or now - _fast_cache[0] >= _FAST_CACHE_TTL_SECONDS:
            _fast_cache = (now, _collect_fast())
        if not _slow_cache or now - _slow_cache[0] >= _SLOW_CACHE_TTL_SECONDS:
            _slow_cache = (now, _collect_slow())
        fast = deepcopy(_fast_cache[1])
        slow = deepcopy(_slow_cache[1])
        with _service_cache_lock:
            current_services = deepcopy(_service_cache[1]) if _service_cache else None
        if current_services is not None:
            slow["services"] = current_services
            service_items = current_services.get("items")
            if isinstance(service_items, list):
                slow.setdefault("integrations", {})["juke"] = _integration_state(
                    service_items, "juke_media_agent", "juke_backend"
                )
                slow.setdefault("integrations", {})["voting"] = _integration_state(
                    service_items, "voting_agent", "voting_backend"
                )
        return {
            "schema_version": 1,
            "generated_at": int(time.time()),
            "freshness": {
                "runtime_observed_at": fast.pop("observed_at", None),
                "slow_observed_at": slow.pop("observed_at", None),
                "service_observed_at": slow.get("services", {}).get(
                    "observed_at"
                ),
            },
            **fast,
            **slow,
        }

@router.get("/api/monitor/snapshot")
@router.get("/api/health-wall", include_in_schema=False)
def health_wall(request: Request) -> dict:
    """GET-only local telemetry; never returns paths, secrets, exceptions or controls."""
    _loopback_only(request)
    return _cached_snapshot()
