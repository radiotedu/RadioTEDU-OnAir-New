import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.ads import router as ads_router
from app.api.ai_host import router as ai_host_router
from app.api.ai_host_fast import router as ai_host_fast_router
from app.api.ai_diagnostics import router as ai_diagnostics_router
from app.api.auth import router as auth_router
from app.api.audio import router as audio_router
from app.api.campaigns import router as campaigns_router
from app.api.watchdog import router as watchdog_router
from app.api.dayparts import router as dayparts_router
from app.api.health import router as health_router
from app.api.health_wall import router as health_wall_router
from app.api.guest_room import router as guest_room_router
from app.api.ha import router as ha_router
from app.api.integrations import router as integrations_router
from app.api.legacy import router as legacy_router
from app.api.library_automation import router as library_automation_router
from app.api.maintenance import router as maintenance_router
from app.api.music_usage import router as music_usage_router
from app.api.outbox import router as outbox_router
from app.api.public import router as public_router
from app.api.queue import router as queue_router
from app.api.recovery import router as recovery_router
from app.api.roles import router as roles_router
from app.api.runtime import router as runtime_router
from app.api.schedule import router as schedule_router
from app.api.streaming import router as streaming_router
from app.api.stream_config import router as stream_config_router
from app.api.shows import router as shows_router
from app.api.soundboard import router as soundboard_router
from app.api.setup import router as setup_router
from app.api.stations import router as stations_router
from app.api.studios import router as studios_router
from app.api.tracks import router as tracks_router
from app.api.users import router as users_router
from app.api.webrtc import router as webrtc_router
from app.auth.dependencies import (
    get_current_user,
    is_public_api_path,
    user_is_allowed_for_request,
)
from app.audio.live_mic_registry import live_mic_registry
from app.audio.guest_audio_registry import guest_audio_registry
from app.config import (
    get_cors_origins,
    get_public_base_url,
    get_security_headers_enabled,
    get_trust_proxy_headers,
)
from app.dependency_bootstrap import bootstrap_dependencies
from app.db import get_connection, init_db
from app.engine.continuity import resolve_station_fallback_uri
from app.engine.playout_state import reconcile_all_startup
from app.middleware.rate_limit import SlidingWindowRateLimiter
from app.repositories.log_repo import LogRepository
from app.services.audit_chain import audit_chain
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_repo import StationRepository
from app.ws.broadcaster import broadcaster, connection_manager
from app.ws.router import router as ws_router
from app.ws.guest_router import router as guest_ws_router
from app.services.ha_coordinator import ha_coordinator
from app.services.recovery_points import recovery_point_service
from app.services.playout_checkpoint import playout_checkpoint_service
from app.version import PRODUCT_VERSION

logger = logging.getLogger("cleanroom.startup")
_INVALID_STATION_QUERY_VALUES = {"", "undefined", "null", "none", "nan"}
_STATION_QUERY_KEYS = {"station_id", "source_station_id", "target_station_id"}
_REQUEST_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUTHY_ENV_VALUES


def _truthy_setting(raw: str, default: bool = False) -> bool:
    token = str(raw if raw is not None else default).strip().lower()
    if token in _TRUTHY_ENV_VALUES:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _any_station_ai_enabled(conn) -> bool:
    station_repo = StationRepository(conn)
    settings_repo = SettingsRepository(conn)
    return any(
        int(row["id"]) > 0
        and _truthy_setting(
            settings_repo.get_station(int(row["id"])).get(
                "ai_host_enabled", "false"
            )
        )
        for row in station_repo.list_all()
    )


def _preload_startup_ai(conn, *, skip_startup_ai: bool) -> bool:
    if skip_startup_ai:
        logger.info("AI live playout preload skipped by CLEANROOM_SKIP_STARTUP_AI")
        return False
    if not _any_station_ai_enabled(conn):
        logger.info("AI live playout preload skipped; AI is disabled for every station")
        return False
    try:
        from app.services.ai_host_fast import get_ai_host_fast

        ai = get_ai_host_fast()
        ai_status = ai.preload_for_playout()
        logger.info(
            "AI live playout preload complete: llm_loaded=%s tts_provider=%s load_time=%.2fs",
            bool(ai_status.get("llm_loaded", False)),
            str(ai_status.get("tts_provider") or ""),
            float(ai_status.get("load_time_seconds") or 0.0),
        )
        return True
    except Exception as exc:
        logger.warning("AI fast host live preload failed: %s", exc)
        return False


def _prime_startup_ai_prefetch(conn, *, skip_startup_ai: bool) -> int:
    station_repo = StationRepository(conn)
    settings_repo = SettingsRepository(conn)
    enabled_stations: list[tuple[int, dict]] = []
    for station in station_repo.list_all():
        station_id = int(station["id"])
        if station_id <= 0:
            continue
        station_settings = settings_repo.get_station(station_id)
        if _truthy_setting(station_settings.get("ai_host_enabled", "false")):
            enabled_stations.append((station_id, station_settings))
            continue
        settings_repo.upsert_station(
            station_id,
            {
                "startup_ai_readiness_state": "disabled",
                "startup_ai_ready_intro_count": "0",
                "startup_ai_required_intro_count": "0",
            },
        )

    if not enabled_stations:
        logger.info("AI prefetch skipped; AI is disabled for every station")
        return 0

    from app.services.ai_prefetch import get_ai_prefetch, startup_buffer_target

    prefetch = None if skip_startup_ai else get_ai_prefetch()
    started = 0
    for station_id, station_settings in enabled_stations:
        required_intros = startup_buffer_target(station_settings)
        try:
            if prefetch is not None:
                prefetch.start(station_id)
                started += 1
            readiness = (
                {"ready_track_intros": 0}
                if prefetch is None
                else prefetch.readiness_snapshot(
                    station_id, lookahead=required_intros
                )
            )
            ready_count = int(readiness.get("ready_track_intros", 0) or 0)
            settings_repo.upsert_station(
                station_id,
                {
                    "startup_ai_readiness_state": (
                        "ready" if ready_count >= required_intros else "warming"
                    ),
                    "startup_ai_ready_intro_count": str(ready_count),
                    "startup_ai_required_intro_count": str(required_intros),
                },
            )
            logger.info(
                "AI prefetch startup for station %d: started=%s readiness=%s",
                station_id,
                prefetch is not None,
                readiness,
            )
        except Exception as prime_exc:
            logger.warning(
                "AI prefetch prime failed for station %d: %s",
                station_id,
                prime_exc,
            )
            settings_repo.upsert_station(
                station_id,
                {
                    "startup_ai_readiness_state": "warming",
                    "startup_ai_ready_intro_count": "0",
                    "startup_ai_required_intro_count": str(required_intros),
                },
            )
    return started


def _autostart_station_worker_loops(conn) -> None:
    repo = StationRepository(conn)
    settings_repo = SettingsRepository(conn)
    rows = list(repo.list_all())
    if not rows:
        station_id = repo.create("Main Radio")
        repo.set_active(station_id)
        rows = list(repo.list_all())
    from app.api.runtime import worker_loop_manager

    for row in rows:
        station_id = int(row["id"])
        if station_id <= 0:
            continue
        station_settings = settings_repo.get_station(station_id)
        if not _truthy_setting(
            station_settings.get("broadcast_autostart_enabled", "false"),
            default=False,
        ):
            logger.info(
                "Station %d worker autostart skipped; an operator has not enabled it",
                station_id,
            )
            continue
        ai_enabled = _truthy_setting(station_settings.get("ai_host_enabled", "false"))
        ai_state = str(station_settings.get("startup_ai_readiness_state", "") or "").strip().lower()
        if ai_enabled and ai_state != "ready":
            logger.warning(
                "Station %d is operator-authorized to autostart; AI is not ready, so deterministic music continuity starts without AI: state=%s ready=%s required=%s",
                station_id,
                ai_state or "unknown",
                station_settings.get("startup_ai_ready_intro_count", "0"),
                station_settings.get("startup_ai_required_intro_count", "0"),
            )
        try:
            fallback_uri = resolve_station_fallback_uri(
                station_id=station_id,
                conn=conn,
            )
            worker_loop_manager.start(
                station_id=station_id,
                fallback_uri=fallback_uri,
                # A one-second scheduler cadence can miss the short transition
                # overlap and expose producer startup as an audible microdrop.
                interval_sec=0.1,
            )
        except Exception as exc:
            # A stale lease, corrupt fallback, or one failed worker must never
            # prevent the other operator-authorized stations from starting at
            # boot. The watchdog can repair the isolated station afterward.
            logger.exception(
                "Station %d worker autostart failed; continuing with siblings: %s",
                station_id,
                exc,
            )


def _run_dependency_bootstrap_background() -> None:
    try:
        bootstrap_summary = bootstrap_dependencies()
        failures = {
            name: details
            for name, details in bootstrap_summary.items()
            if str((details or {}).get("status") or "") == "failed"
        }
        if failures:
            logger.warning("Dependency bootstrap failures: %s", failures)
    except Exception as exc:
        logger.warning("Dependency bootstrap failed in background: %s", exc)


def _run_music_usage_export_background() -> None:
    try:
        from app.services.music_usage import MusicUsageService

        usage_conn = get_connection()
        try:
            export_summary = MusicUsageService(usage_conn).ensure_daily_exports()
            logger.info(
                "Music-use daily export ready: records=%s monthly_close=%s",
                (export_summary.get("daily") or {}).get("record_count", 0),
                bool(export_summary.get("monthly_close")),
            )
        finally:
            usage_conn.close()
    except Exception as exc:
        # The append-only database remains authoritative; the next background
        # pass retries without delaying the control plane or live audio.
        logger.warning("Music-use daily export failed; will retry: %s", exc)


def _autostart_station_worker_loops_background() -> None:
    conn = get_connection()
    try:
        _autostart_station_worker_loops(conn)
    except Exception as exc:
        # One stale station lease must not prevent the API or other stations
        # from becoming available.  The worker manager/watchdog retries it.
        logger.warning("Station worker background autostart failed: %s", exc)
    finally:
        conn.close()


def _broadcast_live_mic_status_event(_event_type: str, station_id: int, snapshot: dict) -> None:
    broadcaster.on_mic_status(int(station_id), dict(snapshot or {}))


def _first_header_token(raw: str) -> str:
    token = str(raw or "").split(",", 1)[0].strip()
    return token


def _resolved_public_origin(request: Request) -> str:
    configured = str(get_public_base_url() or "").strip().rstrip("/")
    if configured:
        return configured

    scheme = str(request.url.scheme or "http").strip() or "http"
    host = str(request.headers.get("host") or request.url.netloc or "").strip()
    if get_trust_proxy_headers():
        forwarded_proto = _first_header_token(request.headers.get("x-forwarded-proto", ""))
        forwarded_host = _first_header_token(request.headers.get("x-forwarded-host", ""))
        if forwarded_proto:
            scheme = forwarded_proto
        if forwarded_host:
            host = forwarded_host

    host = host or "localhost"
    return f"{scheme}://{host}".rstrip("/")


def _trusted_client_host(request: Request) -> str:
    if not get_trust_proxy_headers():
        return str(getattr(request.client, "host", "") or "")
    forwarded_for = _first_header_token(request.headers.get("x-forwarded-for", ""))
    if forwarded_for:
        return forwarded_for
    return str(getattr(request.client, "host", "") or "")


def _sanitize_request_id(raw: str) -> str:
    token = "".join(ch for ch in str(raw or "").strip()[:128] if ch in _REQUEST_ID_ALLOWED)
    return token or uuid.uuid4().hex


@asynccontextmanager
async def lifespan(_app: FastAPI):
    connection_manager.reset()
    live_mic_registry.reset()
    guest_audio_registry.reset()
    live_mic_registry.register_listener(_broadcast_live_mic_status_event)
    shutdown_runtime_registry = None
    shutdown_worker_loop_manager = None
    shutdown_library_watcher = None
    shutdown_product_catalog = None
    threading.Thread(
        target=_run_dependency_bootstrap_background,
        daemon=True,
        name="dependency-bootstrap",
    ).start()
    init_db()
    threading.Thread(
        target=_run_music_usage_export_background,
        daemon=True,
        name="music-usage-export",
    ).start()
    recovery_point_service.start()
    playout_checkpoint_service.start()
    from app.services.program_recording import program_recording_service
    program_recording_service.start_maintenance()
    if not _env_truthy("CLEANROOM_DISABLE_LIBRARY_WATCHER"):
        from app.services.managed_library_watcher import get_managed_library_watcher

        shutdown_library_watcher = get_managed_library_watcher()
        shutdown_library_watcher.start()
    if not _env_truthy("CLEANROOM_DISABLE_PRODUCT_CATALOG"):
        from app.services.product_media_catalog import get_product_media_catalog_service

        shutdown_product_catalog = get_product_media_catalog_service()
        shutdown_product_catalog.start()
    conn = get_connection()
    try:
        summary = reconcile_all_startup(conn)
        if any(int(v) > 0 for v in summary.values()):
            logger.info("Startup reconcile applied: %s", summary)
        try:
            from app.services.radiotedu_service_control import (
                SETTINGS_KEY as RADIOTEDU_SERVICE_SETTINGS_KEY,
                auto_start_enabled,
                load_settings as load_radiotedu_service_settings,
            )

            system_settings = SettingsRepository(conn).get_system()
            service_settings = load_radiotedu_service_settings(
                system_settings.get(RADIOTEDU_SERVICE_SETTINGS_KEY, "")
            )
            started_services = auto_start_enabled(service_settings)
            if started_services:
                logger.info(
                    "RadioTEDU managed services auto-started: %s",
                    ", ".join(started_services),
                )
        except Exception as exc:
            logger.warning("RadioTEDU managed service auto-start failed: %s", exc)
        from app.api.runtime import runtime_registry, worker_loop_manager

        shutdown_runtime_registry = runtime_registry
        shutdown_worker_loop_manager = worker_loop_manager

        def on_ha_role(role: str, _snapshot: dict) -> None:
            if role != "leader":
                from app.services.program_recording import program_recording_service
                program_recording_service.stop_all("leadership_lost")
                worker_loop_manager.stop_all()
                runtime_registry.stop_all()
                return
            leader_conn = get_connection()
            try:
                _autostart_station_worker_loops(leader_conn)
            finally:
                leader_conn.close()
            from app.services.program_recording import program_recording_service
            program_recording_service.resume_replicated_recordings()

        ha_coordinator.register_role_callback(on_ha_role)
        ha_coordinator.start()

        skip_startup_ai = _env_truthy("CLEANROOM_SKIP_STARTUP_AI")
        # Warm optional AI only when an operator enabled it for at least one
        # station. Core radio startup must never require an AI runtime.
        _preload_startup_ai(conn, skip_startup_ai=skip_startup_ai)

        # Prime optional AI intros before worker loops start. When no station
        # enables AI, no AI host or prefetch singleton is created.
        try:
            started_ai_prefetch = _prime_startup_ai_prefetch(
                conn, skip_startup_ai=skip_startup_ai
            )
            logger.info(
                "AI prefetch started for %d enabled station(s)",
                started_ai_prefetch,
            )
        except Exception as exc:
            logger.warning("AI prefetch startup failed: %s", exc)

        if _env_truthy("CLEANROOM_SKIP_WORKER_AUTOSTART"):
            logger.info("Station worker loop autostart skipped by CLEANROOM_SKIP_WORKER_AUTOSTART")
        elif ha_coordinator.snapshot()["enabled"]:
            logger.info("Station worker loop autostart is waiting for a fenced HA leadership lease")
        else:
            threading.Thread(
                target=_autostart_station_worker_loops_background,
                daemon=True,
                name="station-worker-autostart",
            ).start()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        yield
    finally:
        connection_manager.reset()
        live_mic_registry.reset()
        guest_audio_registry.reset()
        from app.services.program_recording import program_recording_service
        program_recording_service.stop_all("service_shutdown")
        program_recording_service.stop_maintenance()
        ha_coordinator.stop()
        ha_coordinator.unregister_role_callback(on_ha_role)
        recovery_point_service.stop()
        playout_checkpoint_service.stop()
        if shutdown_library_watcher is not None:
            shutdown_library_watcher.stop()
        if shutdown_product_catalog is not None:
            shutdown_product_catalog.stop()
        try:
            worker_stop = (
                shutdown_worker_loop_manager.stop_all()
                if shutdown_worker_loop_manager is not None
                else {"stopped": 0}
            )
            runtime_stop = (
                shutdown_runtime_registry.stop_all()
                if shutdown_runtime_registry is not None
                else {"stopped": 0}
            )
            if int(worker_stop.get("stopped", 0)) > 0 or int(
                runtime_stop.get("stopped", 0)
            ) > 0:
                logger.info(
                    "Shutdown cleanup applied: worker_loops=%s runtimes=%s",
                    worker_stop,
                    runtime_stop,
                )
        except Exception:
            # Shutdown cleanup must not raise.
            pass


app = FastAPI(title="RadioTEDU OnAir API", version=PRODUCT_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(health_wall_router)
app.include_router(ha_router)
app.include_router(integrations_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(roles_router)
app.include_router(legacy_router)
app.include_router(library_automation_router)
app.include_router(maintenance_router)
app.include_router(music_usage_router)
app.include_router(stations_router)
app.include_router(studios_router)
app.include_router(guest_room_router)
app.include_router(queue_router)
app.include_router(recovery_router)
app.include_router(runtime_router)
app.include_router(audio_router)
app.include_router(campaigns_router)
app.include_router(watchdog_router)
app.include_router(dayparts_router)
app.include_router(outbox_router)
app.include_router(public_router)
app.include_router(ads_router)
app.include_router(schedule_router)
app.include_router(streaming_router)
app.include_router(stream_config_router)
app.include_router(tracks_router)
app.include_router(shows_router)
app.include_router(soundboard_router)
app.include_router(setup_router)
app.include_router(webrtc_router)
app.include_router(ai_host_router)
app.include_router(ai_host_fast_router)
app.include_router(ai_diagnostics_router)
app.include_router(ws_router)
app.include_router(guest_ws_router)

# Some legacy routes are registered late while the compatibility module is
# imported. Keep the application router synchronized so every legacy endpoint
# remains available from the single canonical RadioTEDU OnAir application.
registered_route_signatures = {
    (getattr(route, "path", ""), frozenset(getattr(route, "methods", None) or ()))
    for route in app.routes
}
for legacy_route in legacy_router.routes:
    signature = (
        getattr(legacy_route, "path", ""),
        frozenset(getattr(legacy_route, "methods", None) or ()),
    )
    if signature not in registered_route_signatures:
        app.router.routes.append(legacy_route)
        registered_route_signatures.add(signature)

login_rate_limiter = SlidingWindowRateLimiter()


def _login_rate_limit_settings() -> tuple[int, int]:
    try:
        max_requests = max(1, int(os.getenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "12")))
    except ValueError:
        max_requests = 12
    try:
        window_seconds = max(1, int(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")))
    except ValueError:
        window_seconds = 60
    return max_requests, window_seconds


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception for %s %s",
        str(request.method or "GET").upper(),
        str(request.url.path or ""),
        exc_info=exc,
    )
    path = str(request.url.path or "")
    request_id = str(getattr(request.state, "request_id", "") or uuid.uuid4().hex)
    if path.startswith("/api/"):
        response = JSONResponse(
            status_code=500,
            content={
                "detail": "internal_server_error",
                "message": "Unexpected server error",
                "request_id": request_id,
            },
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response
    response = JSONResponse(
        status_code=500,
        content={"detail": "internal_server_error"},
    )
    response.headers["X-Request-ID"] = request_id
    return response


def _fallback_station_id() -> int:
    conn = None
    try:
        init_db()
        conn = get_connection()
        repo = StationRepository(conn)
        active = repo.get_active()
        if active is not None:
            active_id = int(active["id"])
            if active_id > 0:
                return active_id
        rows = list(repo.list_all())
        if rows:
            first_id = int(rows[0]["id"])
            if first_id > 0:
                return first_id
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 1


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    incoming = str(request.headers.get("x-request-id", "")).strip()
    request_id = _sanitize_request_id(incoming)
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def security_and_public_origin_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Cleanroom-Public-Origin"] = _resolved_public_origin(request)
    if get_security_headers_enabled():
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Origin-Agent-Cluster", "?1")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(self)",
        )
        response.headers.setdefault(
            "Referrer-Policy",
            "strict-origin-when-cross-origin",
        )
        if str(request.url.scheme or "").lower() == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
    return response


@app.middleware("http")
async def sanitize_station_query_middleware(request: Request, call_next):
    raw_qs = request.scope.get("query_string", b"")
    if raw_qs:
        text = raw_qs.decode("utf-8", errors="ignore")
        pairs = parse_qsl(text, keep_blank_values=True)
        changed = False
        fallback_value: str | None = None
        for idx, (key, value) in enumerate(pairs):
            if key not in _STATION_QUERY_KEYS:
                continue
            token = str(value or "").strip().lower()
            if token in _INVALID_STATION_QUERY_VALUES:
                if fallback_value is None:
                    fallback_value = str(_fallback_station_id())
                pairs[idx] = (key, fallback_value)
                changed = True
        if changed:
            request.scope["query_string"] = urlencode(pairs, doseq=True).encode("utf-8")
    return await call_next(request)


@app.middleware("http")
async def auth_rate_limit_middleware(request: Request, call_next):
    path = str(request.url.path or "")
    method = str(request.method or "GET").upper()
    if path == "/api/auth/login" and method == "POST":
        max_requests, window_seconds = _login_rate_limit_settings()
        client_ip = _trusted_client_host(request) or "unknown"
        limiter_key = f"{client_ip}:{path}"
        if not login_rate_limiter.allow(
            limiter_key,
            limit=max_requests,
            window_sec=window_seconds,
        ):
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many login attempts"},
            )
            request_id = str(
                getattr(request.state, "request_id", "")
                or str(request.headers.get("x-request-id", "")).strip()
                or uuid.uuid4().hex
            )
            request.state.request_id = request_id
            if request_id:
                response.headers["X-Request-ID"] = request_id
            return response
    return await call_next(request)


@app.middleware("http")
async def auth_guard_middleware(request: Request, call_next):
    path = str(request.url.path or "")
    if not path.startswith("/api/") or is_public_api_path(path):
        return await call_next(request)

    try:
        user = await get_current_user(request)
    except HTTPException as exc:
        response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        request_id = str(getattr(request.state, "request_id", "") or "")
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response

    if not user_is_allowed_for_request(user, path, str(request.method or "")):
        response = JSONResponse(status_code=403, content={"detail": "Forbidden"})
        request_id = str(getattr(request.state, "request_id", "") or "")
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response

    request.state.current_user = user
    return await call_next(request)


@app.middleware("http")
async def operation_log_middleware(request: Request, call_next):
    response = await call_next(request)
    method = str(request.method or "GET").upper()
    path = str(request.url.path or "")
    if method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/api/"):
        if not path.startswith("/api/logs"):
            station_id: int | None = None
            station_param = request.query_params.get("station_id")
            if station_param:
                try:
                    station_id = int(station_param)
                except ValueError:
                    station_id = None
            try:
                init_db()
                # Mutation logging is best-effort and must never hold an API
                # acknowledgement behind the normal 30-second SQLite wait.
                with closing(get_connection(timeout_seconds=0.25)) as log_conn:
                    LogRepository(log_conn).add_operation_log(
                        station_id=station_id,
                        message=f"{method} {path}",
                        event_type="http",
                        level="info" if response.status_code < 400 else "error",
                        payload={
                            "status_code": int(response.status_code),
                            "request_id": str(getattr(request.state, "request_id", "") or ""),
                        },
                    )
                user = getattr(request.state, "current_user", None)
                actor_id = int(user.get("id")) if isinstance(user, dict) and user.get("id") else None
                audit_chain.append(
                    category="security",
                    action="api.mutation",
                    station_id=station_id,
                    actor_id=actor_id,
                    payload={
                        "method": method,
                        "path": path,
                        "status_code": int(response.status_code),
                        "request_id": str(getattr(request.state, "request_id", "") or ""),
                    },
                )
            except Exception:
                # Logging must never break the request path.
                pass
    return response


@app.middleware("http")
async def api_cache_control_middleware(request: Request, call_next):
    response = await call_next(request)
    if str(request.url.path or "").startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def frontend_favicon():
    ico_path = _STATIC_DIR / "icons" / "icon.ico"
    if ico_path.exists():
        return FileResponse(ico_path)
    png_path = _STATIC_DIR / "icons" / "icon.png"
    return FileResponse(png_path)


@app.get("/sw.js", include_in_schema=False)
def frontend_service_worker():
    return FileResponse(_STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/", include_in_schema=False)
def frontend_index():
    return FileResponse(_STATIC_DIR / "onair" / "index.html")


@app.get("/app", include_in_schema=False)
@app.get("/app/", include_in_schema=False)
def frontend_app():
    return FileResponse(_STATIC_DIR / "onair" / "index.html")


@app.get("/login.html", include_in_schema=False)
def frontend_login():
    return FileResponse(_STATIC_DIR / "onair" / "index.html")


@app.get("/guest.html", include_in_schema=False)
def frontend_guest():
    return FileResponse(_STATIC_DIR / "guest.html")
