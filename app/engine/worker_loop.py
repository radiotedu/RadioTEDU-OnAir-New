import logging
import os
import threading
import time
import uuid

from app.db import get_connection, init_db
from app.engine.continuity import resolve_station_fallback_uri
from app.engine.station_worker import StationWorker

_log = logging.getLogger("cleanroom.worker_loop")
_FAILURE_BACKOFF_SCHEDULE_SECONDS = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)
_STOP_JOIN_TIMEOUT_SECONDS = 15.0
_WORKER_TICK_STALL_SECONDS = 60.0


def _failure_backoff_seconds(failure_count: int, interval_sec: float) -> float:
    count = max(1, int(failure_count))
    delay = _FAILURE_BACKOFF_SCHEDULE_SECONDS[
        min(count - 1, len(_FAILURE_BACKOFF_SCHEDULE_SECONDS) - 1)
    ]
    return max(
        max(0.1, float(interval_sec)),
        float(delay),
    )


class StationWorkerLoopManager:
    def __init__(
        self,
        runtime_registry,
        worker_factory=None,
        runtime_supervisor=None,
    ):
        self.runtime_registry = runtime_registry
        self.worker_factory = worker_factory or (lambda **kwargs: StationWorker(**kwargs))
        self.runtime_supervisor = runtime_supervisor
        self._lock = threading.Lock()
        self._loops: dict[int, dict] = {}
        # Worker IDs must fence different backend processes.  A station-only
        # identifier lets two concurrent backends renew the same lease.
        self._owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"

    def _start_emergency_fallback(
        self,
        station_id: int,
        fallback_uri: str,
    ) -> dict | None:
        if not fallback_uri:
            return None
        return self.runtime_registry.start_station(
            int(station_id),
            str(fallback_uri),
            stream_title="Continuity audio",
            track_type="startup",
        )

    def _recover_runtime_if_needed(
        self,
        station_id: int,
        fallback_uri: str,
    ) -> dict | None:
        if self.runtime_supervisor is None:
            return None
        report = dict(
            self.runtime_supervisor.evaluate_station(int(station_id)) or {}
        )
        state = self._loops.get(int(station_id))
        if state is not None:
            state["last_supervisor_action"] = str(
                report.get("action") or "none"
            )
            state["last_supervisor_error"] = str(
                report.get("error") or ""
            )
            state["last_supervisor_status"] = str(
                report.get("status") or ""
            )
        if bool(report.get("running", False)):
            if state is not None:
                state["runtime_bad_since"] = None
            return None
        if str(report.get("action") or "") == "restart_last_resort":
            return self._start_emergency_fallback(
                int(station_id),
                str(fallback_uri or ""),
            )
        return None

    def _loop_entry(self, station_id: int):
        with self._lock:
            state = self._loops.get(station_id)
        if not state:
            return
        stop_event: threading.Event = state["stop_event"]
        worker_id = f"loop-{station_id}-{self._owner_id}"
        while not stop_event.is_set():
            with self._lock:
                current_state = self._loops.get(station_id)
            if not current_state:
                return
            interval_sec: float = float(current_state["interval_sec"])
            next_attempt_monotonic = float(current_state.get("next_attempt_monotonic") or 0.0)
            delay_remaining = next_attempt_monotonic - time.monotonic()
            if delay_remaining > 0.0:
                with self._lock:
                    current = self._loops.get(station_id)
                    if current:
                        current["last_heartbeat_monotonic"] = time.monotonic()
                stop_event.wait(min(delay_remaining, interval_sec))
                continue
            fallback_uri: str = str(current_state["fallback_uri"] or "")
            worker = None
            with self._lock:
                current = self._loops.get(station_id)
                if current:
                    started = time.monotonic()
                    current["tick_in_progress"] = True
                    current["tick_started_monotonic"] = started
                    current["last_heartbeat_monotonic"] = started
            try:
                self._recover_runtime_if_needed(station_id, fallback_uri)
                worker = self.worker_factory(
                    station_id=station_id,
                    worker_id=worker_id,
                    runtime_registry=self.runtime_registry,
                    fallback_uri=fallback_uri,
                )
                # Read the durable sequence from this worker's queue connection
                # before it evaluates the queue.  A later mutation must wait for
                # a subsequent tick; otherwise an acknowledgement could claim a
                # revision that this worker has not actually seen.
                queue_repo = getattr(worker, "queue_repo", None)
                evaluated_sequence = (
                    queue_repo.change_sequence(station_id)
                    if queue_repo is not None
                    else int(current_state.get("last_observed_queue_sequence") or 0)
                )
                result = worker.process_once()
                with self._lock:
                    current = self._loops.get(station_id)
                    if current:
                        current["ticks"] += 1
                        current["last_result"] = result
                        current["last_error"] = ""
                        current["failure_count"] = 0
                        current["last_backoff_seconds"] = 0.0
                        current["next_attempt_monotonic"] = 0.0
                        current["last_observed_queue_sequence"] = int(evaluated_sequence)
            except Exception as exc:
                with self._lock:
                    current = self._loops.get(station_id)
                    if current:
                        failure_count = int(current.get("failure_count") or 0) + 1
                        backoff_seconds = _failure_backoff_seconds(
                            failure_count,
                            float(current.get("interval_sec") or interval_sec),
                        )
                        current["ticks"] += 1
                        current["last_error"] = str(exc)
                        current["failure_count"] = failure_count
                        current["last_backoff_seconds"] = backoff_seconds
                        current["next_attempt_monotonic"] = time.monotonic() + backoff_seconds
                        _log.warning(
                            "Station worker loop backing off station_id=%s failure_count=%s delay=%.1fs error=%s",
                            station_id,
                            failure_count,
                            backoff_seconds,
                            exc,
                        )
            finally:
                # Close the per-tick connection to avoid leaking handles.
                try:
                    if worker is not None:
                        worker.conn.close()
                except Exception:
                    pass
                with self._lock:
                    current = self._loops.get(station_id)
                    if current:
                        completed = time.monotonic()
                        current["tick_in_progress"] = False
                        current["last_tick_completed_monotonic"] = completed
                        current["last_heartbeat_monotonic"] = completed
            # ── Fast process-end detection ─────────────────────
            # Poll audio process status every 100 ms so we detect
            # track completion almost instantly (like Liquidsoap)
            # instead of leaving up to a 1 s silence gap.
            if self.runtime_registry.is_process_running(station_id):
                _deadline = time.monotonic() + interval_sec
                while not stop_event.is_set():
                    _remaining = _deadline - time.monotonic()
                    if _remaining <= 0:
                        break
                    if not self.runtime_registry.is_process_running(station_id):
                        break
                    stop_event.wait(min(_remaining, 0.1))
            else:
                stop_event.wait(interval_sec)

    def start(
        self, station_id: int, fallback_uri: str = "", interval_sec: float = 1.0
    ) -> dict:
        safe_interval = max(0.1, float(interval_sec))
        init_db()
        conn = get_connection()
        try:
            resolved_fallback_uri = resolve_station_fallback_uri(
                station_id=station_id,
                conn=conn,
                requested=fallback_uri,
            )
        finally:
            conn.close()
        reused_existing = False
        with self._lock:
            existing = self._loops.get(station_id)
            if existing and existing["thread"].is_alive():
                existing["fallback_uri"] = str(resolved_fallback_uri)
                existing["interval_sec"] = safe_interval
                reused_existing = True
            else:
                stop_event = threading.Event()
                state = {
                    "station_id": station_id,
                    "fallback_uri": str(resolved_fallback_uri),
                    "interval_sec": safe_interval,
                    "stop_event": stop_event,
                    "thread": None,
                    "ticks": 0,
                    "last_observed_queue_sequence": 0,
                    "last_result": None,
                    "last_error": "",
                    "failure_count": 0,
                    "last_backoff_seconds": 0.0,
                    "next_attempt_monotonic": 0.0,
                    "last_supervisor_action": "none",
                    "last_supervisor_error": "",
                    "last_supervisor_status": "",
                    "next_recovery_at": 0.0,
                    "runtime_bad_since": None,
                    "tick_in_progress": False,
                    "tick_started_monotonic": 0.0,
                    "last_tick_completed_monotonic": time.monotonic(),
                    "last_heartbeat_monotonic": time.monotonic(),
                }
                thread = threading.Thread(
                    target=self._loop_entry,
                    args=(station_id,),
                    daemon=True,
                    name=f"station-worker-loop-{station_id}",
                )
                state["thread"] = thread
                self._loops[station_id] = state
                thread.start()

        if reused_existing:
            return self.status(station_id)

        # Flag the station so the first process_once() tick inserts the startup sound
        try:
            from app.repositories.settings_repo import SettingsRepository
            conn = get_connection()
            repo = SettingsRepository(conn)
            repo.upsert_station(station_id, {"_startup_sound_pending": "true"})
            conn.close()
        except Exception as exc:
            _log.warning("Could not set startup-sound pending flag: %s", exc)

        return self.status(station_id)

    def stop(self, station_id: int) -> dict:
        with self._lock:
            state = self._loops.get(station_id)
            if not state:
                return {
                    "station_id": station_id,
                    "running": False,
                    "stopping": False,
                    "interval_sec": None,
                    "ticks": 0,
                    "last_result": None,
                    "last_error": "",
                    "failure_count": 0,
                    "last_backoff_seconds": 0.0,
                    "next_attempt_in_seconds": 0.0,
                    "last_observed_queue_sequence": 0,
                    "tick_in_progress": False,
                    "tick_elapsed_seconds": 0.0,
                    "heartbeat_age_seconds": 0.0,
                    "stalled": False,
                }
            stop_event: threading.Event = state["stop_event"]
            thread: threading.Thread = state["thread"]
            stop_event.set()
        # A tick may be inside validation, database work, or encoder startup.
        # Do not claim the scheduler is stopped merely because its stop flag is
        # set: wait for the in-flight tick to leave, then remove the retired
        # loop so a later operator Start always creates a fresh generation.
        thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            current = self._loops.get(station_id)
            if current is state and not thread.is_alive():
                self._loops.pop(station_id, None)
        return self.status(station_id)

    def status(self, station_id: int) -> dict:
        with self._lock:
            state = self._loops.get(station_id)
            if not state:
                return {
                    "station_id": station_id,
                    "running": False,
                    "stopping": False,
                    "interval_sec": None,
                    "fallback_uri": "",
                    "ticks": 0,
                    "last_result": None,
                    "last_error": "",
                    "failure_count": 0,
                    "last_backoff_seconds": 0.0,
                    "next_attempt_in_seconds": 0.0,
                    "tick_in_progress": False,
                    "tick_elapsed_seconds": 0.0,
                    "heartbeat_age_seconds": 0.0,
                    "stalled": False,
                }
            thread: threading.Thread = state["thread"]
            running = bool(thread and thread.is_alive())
            now = time.monotonic()
            tick_in_progress = bool(state.get("tick_in_progress"))
            tick_elapsed = (
                max(
                    0.0,
                    now - float(state.get("tick_started_monotonic") or now),
                )
                if tick_in_progress
                else 0.0
            )
            heartbeat_age = max(
                0.0,
                now - float(state.get("last_heartbeat_monotonic") or now),
            )
            return {
                "station_id": station_id,
                "running": running,
                "stopping": bool(state["stop_event"].is_set()),
                "interval_sec": float(state["interval_sec"]),
                "fallback_uri": str(state["fallback_uri"] or ""),
                "ticks": int(state["ticks"]),
                "last_result": state["last_result"],
                "last_error": str(state["last_error"] or ""),
                "failure_count": int(state.get("failure_count") or 0),
                "last_backoff_seconds": float(state.get("last_backoff_seconds") or 0.0),
                "next_attempt_in_seconds": max(
                    0.0,
                    float(state.get("next_attempt_monotonic") or 0.0) - time.monotonic(),
                ),
                "last_observed_queue_sequence": int(state.get("last_observed_queue_sequence") or 0),
                "tick_in_progress": tick_in_progress,
                "tick_elapsed_seconds": round(tick_elapsed, 3),
                "heartbeat_age_seconds": round(heartbeat_age, 3),
                "stalled": bool(
                    running
                    and tick_in_progress
                    and tick_elapsed >= _WORKER_TICK_STALL_SECONDS
                ),
            }

    def snapshot(self) -> list[dict]:
        with self._lock:
            station_ids = sorted(int(sid) for sid in self._loops.keys())
        return [self.status(station_id) for station_id in station_ids]

    def stop_all(self) -> dict:
        with self._lock:
            station_ids = [int(sid) for sid in self._loops.keys()]
        stopped = 0
        for station_id in station_ids:
            status = self.stop(station_id)
            if status.get("running") is False:
                stopped += 1
        return {
            "stations": station_ids,
            "stopped": stopped,
        }
