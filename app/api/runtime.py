import threading
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.audio.live_mic_registry import live_mic_registry
from app.audio.guest_audio_registry import guest_audio_registry
from app.db import get_connection, init_db
from app.engine.continuity import resolve_station_fallback_uri
from app.engine.playout_state import PlayoutStateService
from app.engine.station_worker import StationWorker
from app.engine.runtime_registry import StationRuntimeRegistry
from app.engine.runtime_supervisor import RuntimeSupervisor
from app.engine.process_worker_manager import (
    ProcessIsolatedStationWorkerManager,
    process_isolation_enabled,
)
from app.engine.process_runtime_facade import ProcessIsolatedRuntimeFacade
from app.engine.worker_loop import StationWorkerLoopManager
from app.repositories.queue_repo import QueueRepository
from app.ws.broadcaster import broadcaster

router = APIRouter()
_local_runtime_registry = StationRuntimeRegistry(
    live_mic_registry=live_mic_registry,
    guest_audio_registry=guest_audio_registry,
)
if process_isolation_enabled():
    worker_loop_manager = ProcessIsolatedStationWorkerManager(
        runtime_registry=_local_runtime_registry,
        live_mic_registry=live_mic_registry,
        guest_audio_registry=guest_audio_registry,
    )
    runtime_registry = ProcessIsolatedRuntimeFacade(
        _local_runtime_registry,
        worker_loop_manager,
    )
    runtime_supervisor = RuntimeSupervisor(runtime_registry)
    worker_loop_manager.runtime_supervisor = runtime_supervisor
else:
    runtime_registry = _local_runtime_registry
    runtime_supervisor = RuntimeSupervisor(runtime_registry)
    worker_loop_manager = StationWorkerLoopManager(
        runtime_registry=runtime_registry,
        runtime_supervisor=runtime_supervisor,
    )
_playout_operations_guard = threading.Lock()
_playout_operations: dict[int, dict] = {}


@contextmanager
def _serialized_playout_operation(station_id: int):
    """Serialize operator playout ownership and issue a monotonically new generation."""
    sid = int(station_id)
    with _playout_operations_guard:
        state = _playout_operations.setdefault(
            sid, {"lock": threading.RLock(), "generation": 0}
        )
    with state["lock"]:
        state["generation"] = int(state["generation"]) + 1
        yield int(state["generation"])


def _resume_scheduler(station_id: int, *, fallback_uri: str, interval_sec: float) -> bool:
    try:
        worker_loop_manager.start(
            station_id=int(station_id),
            fallback_uri=str(fallback_uri or ""),
            interval_sec=float(interval_sec or 1.0),
        )
        return bool(worker_loop_manager.status(int(station_id)).get("running"))
    except Exception:
        return False


def _restart_prior_runtime(station_id: int, prior: dict) -> bool:
    """Recover the interrupted item only when its original input is known."""
    input_uri = str(prior.get("active_input_uri") or "").strip()
    if not input_uri:
        return False
    try:
        runtime_registry.start_station(
            station_id=int(station_id),
            input_uri=input_uri,
            stream_title=str(prior.get("active_stream_title") or ""),
            stream_artist=str(prior.get("active_stream_artist") or ""),
            track_type=str(prior.get("active_track_type") or "music"),
            crossfade_seconds=0.0,
        )
        return bool(runtime_registry.is_process_running(int(station_id)))
    except Exception:
        return False


class RuntimeStartPayload(BaseModel):
    input_uri: str
    stream_title: str = ""
    stream_artist: str = ""


class RuntimeTickPayload(BaseModel):
    fallback_uri: str = ""


class RuntimeLoopStartPayload(BaseModel):
    fallback_uri: str = ""
    interval_sec: float = 0.1


class OperatorSkipPayload(BaseModel):
    item_id: int
    expected_revision: str


def _station_broadcast_autostart_enabled(conn, station_id: int) -> bool:
    row = conn.execute(
        "SELECT value FROM station_settings "
        "WHERE station_id=? AND key='broadcast_autostart_enabled'",
        (int(station_id),),
    ).fetchone()
    value = str(row["value"] if row else "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _require_unattended_start_authorization(station_id: int) -> None:
    """Reject automatic start paths unless the operator explicitly opted in."""
    conn = get_connection()
    try:
        authorized = _station_broadcast_autostart_enabled(conn, station_id)
    finally:
        conn.close()
    if not authorized:
        raise HTTPException(
            status_code=409,
            detail=(
                "operator_authorization_required: automatic callers cannot "
                "start this station while broadcast restart is disabled"
            ),
        )


def _start_runtime_loop(station_id: int, payload: RuntimeLoopStartPayload) -> dict:
    conn = get_connection()
    try:
        fallback_uri = resolve_station_fallback_uri(
            station_id=station_id,
            conn=conn,
            requested=payload.fallback_uri,
        )
    finally:
        conn.close()
    worker_loop_manager.start(
        station_id=station_id,
        fallback_uri=fallback_uri,
        interval_sec=payload.interval_sec,
    )
    response = _runtime_loop_payload(station_id)
    _broadcast_runtime_events(station_id)
    return response


def _preserve_operator_playout(station_id: int) -> dict:
    """Requeue interrupted items without deleting or advancing the program."""
    init_db()
    conn = get_connection()
    try:
        sid = int(station_id)
        state_service = PlayoutStateService(conn)
        previous = state_service.get_current(sid)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM queue_items "
            "WHERE station_id=? AND status IN ('pending','playing')",
            (sid,),
        )
        queue_before = int(cur.fetchone()["c"] or 0)
        cur.execute(
            "UPDATE queue_items SET status='pending', started_at=NULL, finished_at=NULL "
            "WHERE station_id=? AND status='playing'",
            (sid,),
        )
        queue_requeued = int(cur.rowcount or 0)
        cur.execute(
            "UPDATE ad_break_items SET status='pending', started_at=NULL, finished_at=NULL "
            "WHERE station_id=? AND status='playing'",
            (sid,),
        )
        ads_requeued = int(cur.rowcount or 0)
        cur.execute(
            "UPDATE schedule_items SET status='pending' "
            "WHERE station_id=? AND status='playing'",
            (sid,),
        )
        schedules_requeued = int(cur.rowcount or 0)
        state_service.set_current(
            sid,
            "none",
            None,
            reason="operator_stop_preserve_playlist",
        )
        cur.execute(
            "SELECT COUNT(*) AS c FROM queue_items "
            "WHERE station_id=? AND status IN ('pending','playing')",
            (sid,),
        )
        queue_after = int(cur.fetchone()["c"] or 0)
        return {
            "playlist_preserved": queue_after == queue_before,
            "queue_items_before": queue_before,
            "queue_items_after": queue_after,
            "queue_items_requeued": queue_requeued,
            "ads_requeued": ads_requeued,
            "schedules_requeued": schedules_requeued,
            "previous_source": str(previous.get("source") or "none"),
            "previous_item_id": previous.get("item_id"),
            "resume_behavior": (
                "The interrupted item remains in place and restarts from its beginning."
            ),
        }
    finally:
        conn.close()


def _runtime_status_payload(station_id: int) -> dict:
    payload = dict(runtime_registry.status(station_id=station_id))
    payload["worker_loop"] = worker_loop_manager.status(station_id=station_id)
    return payload


def _runtime_loop_payload(station_id: int) -> dict:
    payload = dict(worker_loop_manager.status(station_id=station_id))
    payload["runtime"] = runtime_registry.status(station_id=station_id)
    return payload


def _broadcast_runtime_events(station_id: int, *, include_queue: bool = False, include_track: bool = False) -> None:
    try:
        from app.api.legacy import legacy_liquidsoap_status, list_legacy_queue

        status_payload = legacy_liquidsoap_status(station_id=station_id)
        broadcaster.on_runtime_updated(station_id, status_payload)
        broadcaster.on_engine_event(station_id, status_payload)
        broadcaster.on_health_changed(
            station_id,
            {
                "station_id": int(station_id),
                "active_station_id": int(status_payload.get("active_station_id") or station_id),
                "engine_running": bool(status_payload.get("alive")),
                "liquidsoap_connected": bool(status_payload.get("liquidsoap_connected")),
            },
        )
        if include_track:
            broadcaster.on_track_changed(station_id, status_payload)
        if include_queue:
            broadcaster.on_queue_changed(station_id, list_legacy_queue(station_id))
    except Exception:
        # WebSocket fan-out must never break HTTP endpoints.
        pass


@router.post("/api/runtime/{station_id}/start")
def start_runtime(station_id: int, payload: RuntimeStartPayload):
    _require_unattended_start_authorization(station_id)
    with _serialized_playout_operation(station_id):
        return _start_runtime(station_id, payload)


@router.post("/api/runtime/{station_id}/operator-start-track")
def operator_start_runtime(station_id: int, payload: RuntimeStartPayload):
    """Explicit operator path for applying or starting the selected program item."""
    with _serialized_playout_operation(station_id):
        return _start_runtime(station_id, payload)


def _start_runtime(station_id: int, payload: RuntimeStartPayload) -> dict:
    try:
        runtime_registry.start_station(
            station_id=station_id,
            input_uri=payload.input_uri,
            stream_title=payload.stream_title,
            stream_artist=payload.stream_artist,
        )
        response = _runtime_status_payload(station_id)
        _broadcast_runtime_events(station_id, include_track=True)
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GStreamer runtime binary not found: {exc}",
        ) from exc


@router.post("/api/runtime/{station_id}/stop")
def stop_runtime(station_id: int):
    with _serialized_playout_operation(station_id):
        runtime_registry.stop_station(station_id=station_id)
        response = _runtime_status_payload(station_id)
    _broadcast_runtime_events(station_id)
    return response


@router.post("/api/runtime/{station_id}/operator-stop")
def operator_stop_runtime(station_id: int):
    with _serialized_playout_operation(station_id) as generation:
        loop_state = worker_loop_manager.stop(station_id=station_id)
        if bool(loop_state.get("running")):
            raise HTTPException(
                status_code=409,
                detail="Scheduler did not stop; playlist state was not changed.",
            )
        runtime_before = runtime_registry.status(station_id=station_id)
        runtime_registry.stop_station(station_id=station_id)
        preservation = _preserve_operator_playout(station_id)
        response = _runtime_status_payload(station_id)
        response.update(preservation)
        response["runtime_was_running"] = bool(
            runtime_before.get("running") or runtime_before.get("program_running")
        )
        response["playout_operation_generation"] = generation
    _broadcast_runtime_events(station_id, include_queue=True, include_track=True)
    return response


@router.post("/api/runtime/{station_id}/operator-skip-current")
def operator_skip_current(station_id: int, payload: OperatorSkipPayload):
    """Safely end the current queue item without allowing concurrent advancement."""
    with _serialized_playout_operation(station_id) as generation:
        loop_before = worker_loop_manager.status(station_id)
        if not bool(loop_before.get("running")):
            raise HTTPException(status_code=409, detail="scheduler is not running; current audio cannot be skipped safely")
        stopped = worker_loop_manager.stop(station_id)
        if bool(stopped.get("running")):
            raise HTTPException(status_code=409, detail="scheduler did not stop; skip was not attempted")

        fallback_uri = str(loop_before.get("fallback_uri") or "")
        interval_sec = float(loop_before.get("interval_sec") or 1.0)
        prior_runtime = dict(runtime_registry.status(station_id))
        runtime_stopped = False
        committed = False
        conn = None
        repo = None
        failure: HTTPException | None = None
        try:
            init_db()
            conn = get_connection()
            repo = QueueRepository(conn)
            # BEGIN IMMEDIATE is intentionally held while the old runtime is
            # stopped.  This makes the revision/current-row validation final.
            outcome = repo.begin_skip_playing_item(
                station_id=station_id,
                item_id=payload.item_id,
                expected_revision=payload.expected_revision,
            )
            if outcome != "ok":
                failure = HTTPException(status_code=409, detail="queue changed; skip was not applied")
                raise failure
            if not runtime_registry.is_process_running(station_id):
                failure = HTTPException(status_code=409, detail="no active runtime audio to skip")
                raise failure
            runtime_registry.stop_station(station_id)
            runtime_stopped = not runtime_registry.is_process_running(station_id)
            if not runtime_stopped:
                failure = HTTPException(status_code=503, detail="runtime did not stop; skip was not applied")
                raise failure
            outcome = repo.commit_skip_playing_item(
                station_id=station_id,
                item_id=payload.item_id,
            )
            if outcome != "ok":
                failure = HTTPException(status_code=409, detail="queue changed; skip was not applied")
                raise failure
            committed = True
        except HTTPException as exc:
            failure = exc
        except Exception as exc:
            failure = HTTPException(status_code=503, detail="skip persistence failed; broadcast recovery required")
        finally:
            if repo is not None and not committed:
                repo.rollback_skip_playing_item()
            if conn is not None:
                conn.close()

        if failure is not None:
            # A failed commit must never leave a 'playing' DB row paired with a
            # dead runtime.  Restore that exact input before resuming the old
            # scheduler; if restoration fails, leave the station safely stopped.
            if runtime_stopped and not _restart_prior_runtime(station_id, prior_runtime):
                raise HTTPException(
                    status_code=503,
                    detail="skip was not persisted; prior runtime recovery failed and station is safely stopped",
                ) from failure
            if not _resume_scheduler(
                station_id, fallback_uri=fallback_uri, interval_sec=interval_sec
            ):
                raise HTTPException(
                    status_code=503,
                    detail="skip was not persisted; scheduler recovery failed and station is safely stopped",
                ) from failure
            raise failure

        if not _resume_scheduler(
            station_id, fallback_uri=fallback_uri, interval_sec=interval_sec
        ):
            # The old item is durably complete, so never restart it here.  The
            # station remains safely stopped for an operator to resume.
            raise HTTPException(
                status_code=503,
                detail="skip persisted but scheduler resume failed; station left safely stopped",
            )
    _broadcast_runtime_events(station_id, include_queue=True, include_track=True)
    return {
        "ok": True,
        "skipped_item_id": int(payload.item_id),
        "runtime_stopped": True,
        "scheduler_resumed": bool(worker_loop_manager.status(station_id).get("running")),
        "playout_operation_generation": generation,
    }


@router.get("/api/runtime/{station_id}/status")
def runtime_status(station_id: int):
    return _runtime_status_payload(station_id)


@router.get("/api/runtime/{station_id}/transitions")
def runtime_transitions(station_id: int, limit: int = 100):
    init_db()
    conn = get_connection()
    try:
        return {
            "station_id": int(station_id),
            "items": PlayoutStateService(conn).list_recent(
                int(station_id),
                limit=limit,
            ),
        }
    finally:
        conn.close()


@router.post("/api/runtime/{station_id}/supervise")
def supervise_runtime(station_id: int):
    _require_unattended_start_authorization(station_id)
    return runtime_supervisor.evaluate_station(station_id=station_id)


@router.post("/api/runtime/{station_id}/operator-supervise")
def operator_supervise_runtime(station_id: int):
    """Explicit operator path for an immediate recovery evaluation."""
    return runtime_supervisor.evaluate_station(station_id=station_id)


@router.post("/api/runtime/{station_id}/tick")
def tick_runtime(station_id: int, payload: RuntimeTickPayload):
    _require_unattended_start_authorization(station_id)
    try:
        conn = get_connection()
        try:
            fallback_uri = resolve_station_fallback_uri(
                station_id=station_id,
                conn=conn,
                requested=payload.fallback_uri,
            )
        finally:
            conn.close()
        worker = StationWorker(
            station_id=station_id,
            runtime_registry=runtime_registry,
            fallback_uri=fallback_uri,
        )
        response = worker.process_once()
        _broadcast_runtime_events(
            station_id,
            include_queue=True,
            include_track=bool(response.get("input_uri")),
        )
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GStreamer runtime binary not found: {exc}",
        ) from exc


@router.post("/api/runtime/{station_id}/loop/start")
def start_runtime_loop(station_id: int, payload: RuntimeLoopStartPayload):
    _require_unattended_start_authorization(station_id)
    with _serialized_playout_operation(station_id):
        return _start_runtime_loop(station_id, payload)


@router.post("/api/runtime/{station_id}/operator-start")
def operator_start_runtime_loop(station_id: int, payload: RuntimeLoopStartPayload):
    """Explicit operator Start path; never available to unattended guards."""
    with _serialized_playout_operation(station_id):
        return _start_runtime_loop(station_id, payload)


@router.post("/api/runtime/{station_id}/loop/stop")
def stop_runtime_loop(station_id: int):
    with _serialized_playout_operation(station_id) as generation:
        worker_loop_manager.stop(station_id=station_id)
        response = _runtime_loop_payload(station_id)
        response["playout_operation_generation"] = generation
    _broadcast_runtime_events(station_id)
    return response


@router.get("/api/runtime/{station_id}/loop/status")
def runtime_loop_status(station_id: int):
    return _runtime_loop_payload(station_id)
