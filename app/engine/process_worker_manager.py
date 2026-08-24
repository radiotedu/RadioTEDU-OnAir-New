from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path

from app.config import get_data_root, get_db_path, get_user_config_root
from app.db import get_connection, init_db
from app.engine.continuity import resolve_station_fallback_uri
from app.engine.process_audio_bridge import ProcessAudioBridgeHost
from app.engine.worker_loop import _failure_backoff_seconds

_log = logging.getLogger("cleanroom.process_worker_manager")
_STARTUP_TIMEOUT_SECONDS = 15.0
_STOP_TIMEOUT_SECONDS = 15.0
_HEARTBEAT_STALL_SECONDS = 60.0
_RESTART_WINDOW_SECONDS = 600.0
_RESTART_BUDGET = 5
_CIRCUIT_OPEN_SECONDS = 300.0
# An isolated worker writes its heartbeat after a complete scheduler tick.
# File probing, AI preparation, or a slow network filesystem can legitimately
# stretch a tick beyond ten seconds.  Using a shorter adoption window than the
# watchdog stall window caused healthy autonomous workers to be replaced while
# they still owned the station lease.
_ADOPTION_HEARTBEAT_MAX_AGE_SECONDS = _HEARTBEAT_STALL_SECONDS


def process_isolation_enabled() -> bool:
    raw = os.getenv("RADIOTEDU_PROCESS_ISOLATED_WORKERS", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return bool(getattr(sys, "frozen", False))


class ProcessIsolatedStationWorkerManager:
    def __init__(
        self,
        runtime_registry,
        runtime_supervisor=None,
        *,
        live_mic_registry=None,
        guest_audio_registry=None,
    ):
        self.runtime_registry = runtime_registry
        self.runtime_supervisor = runtime_supervisor
        self.live_mic_registry = live_mic_registry
        self.guest_audio_registry = guest_audio_registry
        self._lock = threading.RLock()
        self._states: dict[int, dict] = {}
        self._owner_id = f"{os.getpid()}-{secrets.token_hex(8)}"
        self._state_root = get_data_root() / "State" / "StationWorkers"
        self._log_root = get_data_root() / "Logs" / "StationWorkers"

    def _rpc_address(self, station_id: int, generation: int) -> tuple[str, str]:
        token = secrets.token_hex(8)
        if os.name == "nt":
            return (
                rf"\\.\pipe\radiotedu-onair-{os.getpid()}-{station_id}-{generation}-{token}",
                "AF_PIPE",
            )
        address = Path("/tmp") / (
            f"radiotedu-onair-{os.getpid()}-{station_id}-{generation}-{token}.sock"
        )
        return str(address), "AF_UNIX"

    def _child_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "station-worker-process"]
        return [sys.executable, "-m", "app.station_worker_process"]

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _heartbeat_path(self, station_id: int) -> Path:
        return self._state_root / f"station-{int(station_id)}.heartbeat.json"

    def _stop_path(self, station_id: int) -> Path:
        return self._state_root / f"station-{int(station_id)}.stop"

    def _lock_path(self, station_id: int) -> Path:
        return self._state_root / f"station-{int(station_id)}.lease"

    def _command_path(self, station_id: int) -> Path:
        return self._state_root / f"station-{int(station_id)}.command.json"

    def _ack_path(self, station_id: int) -> Path:
        return self._state_root / f"station-{int(station_id)}.ack.json"

    def _audio_bridge_path(self, station_id: int) -> Path:
        return self._state_root / f"station-{int(station_id)}.audio-bridge.bin"

    def _ensure_audio_bridge(self, state: dict) -> ProcessAudioBridgeHost:
        host = state.get("audio_bridge_host")
        if host is not None:
            return host
        station_id = int(state["station_id"])
        path = Path(
            state.get("audio_bridge_path") or self._audio_bridge_path(station_id)
        )
        settings_provider = getattr(
            self.runtime_registry,
            "get_live_audio_settings",
            None,
        )
        host = ProcessAudioBridgeHost(
            path,
            station_id,
            live_mic_registry=self.live_mic_registry,
            guest_audio_registry=self.guest_audio_registry,
            live_settings_provider=settings_provider,
        )
        host.start()
        state["audio_bridge_host"] = host
        state["audio_bridge_path"] = path
        return host

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload) if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        candidate = int(pid or 0)
        if candidate <= 0:
            return False
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                candidate,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle,
                    ctypes.byref(exit_code),
                ):
                    return False
                return int(exit_code.value) == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        try:
            os.kill(candidate, 0)
        except (OSError, ProcessLookupError, PermissionError):
            return False
        return True

    def _read_live_heartbeat(self, station_id: int) -> dict:
        payload = self._read_json(self._heartbeat_path(station_id))
        if int(payload.get("station_id") or 0) != int(station_id):
            return {}
        pid = int(payload.get("pid") or 0)
        updated_epoch = float(payload.get("updated_epoch") or 0.0)
        age = max(0.0, time.time() - updated_epoch) if updated_epoch else float("inf")
        if not self._pid_is_alive(pid) or age > _ADOPTION_HEARTBEAT_MAX_AGE_SECONDS:
            return {}
        payload["heartbeat_age_seconds"] = age
        return payload

    def _refresh_from_file_heartbeat(
        self,
        state: dict,
        *,
        expected_pid: int | None = None,
    ) -> bool:
        """Refresh watchdog state from the worker-owned atomic heartbeat.

        The authenticated RPC connection belongs to one backend process and is
        expected to disappear during a backend restart.  A station worker must
        not be terminated merely because that reverse channel is gone while its
        fenced file heartbeat still proves that the same generation is alive.
        """
        station_id = int(state["station_id"])
        heartbeat = self._read_live_heartbeat(station_id)
        if not heartbeat:
            return False
        heartbeat_pid = int(heartbeat.get("pid") or 0)
        if expected_pid is not None and heartbeat_pid != int(expected_pid):
            return False
        expected_generation = int(state.get("generation") or 0)
        heartbeat_generation = int(heartbeat.get("generation") or 0)
        if expected_generation and heartbeat_generation != expected_generation:
            return False
        if bool(heartbeat.get("scheduler_stalled")) and not bool(
            heartbeat.get("transport_healthy")
        ):
            # Independent liveness must preserve a healthy encoder, not mask a
            # scheduler stall that has also stopped current program audio.
            return False
        with self._lock:
            state["last_heartbeat_epoch"] = float(
                heartbeat.get("updated_epoch") or time.time()
            )
            state["last_heartbeat_monotonic"] = time.monotonic()
            state["last_result"] = heartbeat.get("last_result")
            state["last_runtime_status"] = heartbeat.get("runtime_status")
            state["failure_count"] = int(heartbeat.get("failure_count") or 0)
            state["last_backoff_seconds"] = float(
                heartbeat.get("last_backoff_seconds") or 0.0
            )
            state["ticks"] = int(heartbeat.get("ticks") or 0)
        return True

    def _adopt_existing_state(
        self,
        station_id: int,
        *,
        fallback_uri: str,
        interval_sec: float,
    ) -> dict | None:
        heartbeat = self._read_live_heartbeat(station_id)
        if not heartbeat:
            return None
        ready_event = threading.Event()
        ready_event.set()
        now = time.monotonic()
        return {
            "adopted": True,
            "adopted_pid": int(heartbeat["pid"]),
            "ack_path": self._ack_path(station_id),
            "audio_bridge_path": self._audio_bridge_path(station_id),
            "circuit_open_until": 0.0,
            "circuit_state": "closed",
            "command_lock": threading.Lock(),
            "command_path": self._command_path(station_id),
            "desired_running": True,
            "failure_count": int(heartbeat.get("failure_count") or 0),
            "fallback_uri": str(fallback_uri or heartbeat.get("fallback_uri") or ""),
            "generation": int(heartbeat.get("generation") or 1),
            "heartbeat_path": self._heartbeat_path(station_id),
            "interval_sec": float(interval_sec or heartbeat.get("interval_sec") or 1.0),
            "last_backoff_seconds": float(heartbeat.get("last_backoff_seconds") or 0.0),
            "last_error": str(heartbeat.get("last_error") or ""),
            "last_heartbeat_epoch": float(heartbeat.get("updated_epoch") or time.time()),
            "last_heartbeat_monotonic": now,
            "last_result": heartbeat.get("last_result"),
            "last_runtime_status": heartbeat.get("runtime_status"),
            "lock_path": self._lock_path(station_id),
            "process": None,
            "ready_event": ready_event,
            "restart_count": int(heartbeat.get("restart_count") or 0),
            "restart_failures": [],
            "station_id": int(station_id),
            "stop_path": self._stop_path(station_id),
            "ticks": int(heartbeat.get("ticks") or 0),
            "watchdog_terminations": 0,
        }

    def _spawn_generation(self, state: dict) -> threading.Event:
        station_id = int(state["station_id"])
        self._ensure_audio_bridge(state)
        with self._lock:
            state["generation"] = int(state.get("generation") or 0) + 1
            generation = int(state["generation"])
            ready_event = threading.Event()
            state["ready_event"] = ready_event
            state["last_heartbeat_monotonic"] = time.monotonic()
            state["last_error"] = ""
            state["circuit_state"] = (
                "half_open" if state.get("circuit_state") == "open" else "closed"
            )

        self._state_root.mkdir(parents=True, exist_ok=True)
        self._log_root.mkdir(parents=True, exist_ok=True)
        config_path = self._state_root / f"station-{station_id}-g{generation}.json"
        stop_path = self._stop_path(station_id)
        heartbeat_path = self._heartbeat_path(station_id)
        lock_path = self._lock_path(station_id)
        command_path = self._command_path(station_id)
        ack_path = self._ack_path(station_id)
        stop_path.unlink(missing_ok=True)
        command_path.unlink(missing_ok=True)
        ack_path.unlink(missing_ok=True)
        address, family = self._rpc_address(station_id, generation)
        authkey = secrets.token_bytes(32)
        listener = Listener(address, family=family, authkey=authkey)
        worker_id = (
            f"process-{station_id}-{self._owner_id}-g{generation}"
        )
        config = {
            "data_root": str(get_data_root()),
            "database_path": str(get_db_path()),
            "ack_path": str(ack_path),
            "audio_bridge_path": str(
                state.get("audio_bridge_path") or self._audio_bridge_path(station_id)
            ),
            "command_path": str(command_path),
            "fallback_uri": str(state.get("fallback_uri") or ""),
            "generation": generation,
            "heartbeat_path": str(heartbeat_path),
            "interval_sec": float(state.get("interval_sec") or 1.0),
            "lock_path": str(lock_path),
            "log_path": str(self._log_root / f"station-{station_id}.log"),
            "rpc_address": address,
            "rpc_authkey": base64.b64encode(authkey).decode("ascii"),
            "rpc_family": family,
            "schema_version": 2,
            "station_id": station_id,
            "stop_path": str(stop_path),
            "user_config_root": str(get_user_config_root()),
            "worker_id": worker_id,
        }
        self._atomic_write_json(config_path, config)

        environment = os.environ.copy()
        environment["RADIOTEDU_STATION_WORKER_CONFIG"] = str(config_path)
        environment["RADIOTEDU_PROCESS_ISOLATED_WORKERS"] = "0"
        environment["CLEANROOM_OPEN_PANEL"] = "0"
        creation_flags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            | int(getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0))
            if os.name == "nt"
            else 0
        )
        process = subprocess.Popen(
            self._child_command(),
            cwd=str(Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        server_thread = threading.Thread(
            target=self._serve_generation,
            args=(state, generation, listener),
            daemon=True,
            name=f"station-worker-rpc-{station_id}-g{generation}",
        )
        with self._lock:
            state.update(
                {
                    "config_path": config_path,
                    "ack_path": ack_path,
                    "command_lock": state.get("command_lock") or threading.Lock(),
                    "command_path": command_path,
                    "heartbeat_path": heartbeat_path,
                    "listener": listener,
                    "lock_path": lock_path,
                    "process": process,
                    "worker_pid": None,
                    "rpc_address": address,
                    "server_thread": server_thread,
                    "stop_path": stop_path,
                }
            )
        server_thread.start()
        return ready_event

    def _validate_station_argument(self, state: dict, args: list) -> int:
        if not args:
            raise RuntimeError("station worker RPC omitted station id")
        station_id = int(args[0])
        if station_id != int(state["station_id"]):
            raise RuntimeError("station worker RPC crossed station boundary")
        return station_id

    def _handle_rpc(self, state: dict, request: dict):
        method = str(request.get("method") or "")
        args = list(request.get("args") or [])
        kwargs = dict(request.get("kwargs") or {})
        station_id = self._validate_station_argument(state, args)
        if method == "heartbeat":
            payload = dict(args[1] if len(args) > 1 else {})
            with self._lock:
                reported_pid = int(payload.get("pid") or 0)
                if reported_pid > 0:
                    state["worker_pid"] = reported_pid
                state["last_heartbeat_monotonic"] = time.monotonic()
                state["last_heartbeat_epoch"] = time.time()
                state["ticks"] = max(0, int(payload.get("ticks") or 0))
                state["last_result"] = payload.get("last_result")
                state["last_runtime_status"] = payload.get("runtime_status")
                state["last_error"] = str(payload.get("last_error") or "")[:120]
                state["failure_count"] = max(
                    0, int(payload.get("failure_count") or 0)
                )
                state["last_backoff_seconds"] = max(
                    0.0, float(payload.get("last_backoff_seconds") or 0.0)
                )
                if str(payload.get("event") or "") == "ready":
                    state["ready_event"].set()
                    if state.get("circuit_state") == "half_open":
                        state["circuit_state"] = "closed"
            return {"accepted": True}
        if method == "start_station":
            return self.runtime_registry.start_station(*args, **kwargs)
        if method == "stop_station":
            return self.runtime_registry.stop_station(station_id)
        if method == "status":
            return self.runtime_registry.status(station_id)
        if method == "is_process_running":
            return self.runtime_registry.is_process_running(station_id)
        if method == "evaluate_station":
            if self.runtime_supervisor is None:
                return {"action": "none", "running": True, "status": "unmanaged"}
            return self.runtime_supervisor.evaluate_station(station_id)
        raise RuntimeError("station worker RPC method is not allowed")

    def _serve_generation(self, state: dict, generation: int, listener: Listener) -> None:
        connection = None
        try:
            connection = listener.accept()
            while True:
                request = dict(connection.recv() or {})
                try:
                    result = self._handle_rpc(state, request)
                    connection.send({"ok": True, "result": result})
                except Exception as exc:
                    _log.warning(
                        "station worker RPC rejected station_id=%s code=%s",
                        state["station_id"],
                        type(exc).__name__,
                    )
                    connection.send(
                        {"error": f"RPC_{type(exc).__name__.upper()}", "ok": False}
                    )
        except (EOFError, OSError):
            pass
        finally:
            if connection is not None:
                connection.close()
            listener.close()
            if os.name != "nt":
                Path(str(getattr(listener, "address", ""))).unlink(missing_ok=True)

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)

    @classmethod
    def _terminate_pid(cls, pid: int) -> bool:
        """Terminate one previously authenticated adopted worker PID.

        The PID originates from a valid station heartbeat and is retained in
        the manager state.  This method is used only after the shared stop file
        and a graceful wait have failed; it prevents a frozen adopted process
        from holding the one-source station lease forever.
        """

        candidate = int(pid or 0)
        if candidate <= 0 or not cls._pid_is_alive(candidate):
            return True
        try:
            os.kill(candidate, signal.SIGTERM)
        except (OSError, ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 5.0
        while cls._pid_is_alive(candidate) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not cls._pid_is_alive(candidate):
            return True

        try:
            if os.name == "nt":
                process_terminate = 0x0001
                handle = ctypes.windll.kernel32.OpenProcess(
                    process_terminate,
                    False,
                    candidate,
                )
                if handle:
                    try:
                        ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    finally:
                        ctypes.windll.kernel32.CloseHandle(handle)
            else:
                os.kill(candidate, signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 5.0
        while cls._pid_is_alive(candidate) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not cls._pid_is_alive(candidate)

    @staticmethod
    def _cleanup_generation(state: dict) -> None:
        listener = state.get("listener")
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for path_name in ("ack_path", "command_path", "config_path", "stop_path"):
            path = state.get(path_name)
            if path:
                Path(path).unlink(missing_ok=True)

    def _monitor_adopted(self, state: dict) -> None:
        station_id = int(state["station_id"])
        while True:
            with self._lock:
                if not bool(state.get("desired_running")):
                    return
            heartbeat = self._read_live_heartbeat(station_id)
            if heartbeat:
                with self._lock:
                    state["adopted_pid"] = int(heartbeat["pid"])
                    state["failure_count"] = int(
                        heartbeat.get("failure_count") or 0
                    )
                    state["last_backoff_seconds"] = float(
                        heartbeat.get("last_backoff_seconds") or 0.0
                    )
                    state["last_error"] = str(
                        heartbeat.get("last_error") or ""
                    )[:120]
                    state["last_heartbeat_epoch"] = float(
                        heartbeat.get("updated_epoch") or time.time()
                    )
                    state["last_heartbeat_monotonic"] = time.monotonic()
                    state["last_result"] = heartbeat.get("last_result")
                    state["last_runtime_status"] = heartbeat.get(
                        "runtime_status"
                    )
                    state["ticks"] = int(heartbeat.get("ticks") or 0)
                time.sleep(0.25)
                continue

            with self._lock:
                if not bool(state.get("desired_running")):
                    return
                adopted_pid = int(state.get("adopted_pid") or 0)

            # The heartbeat has exceeded the same 60-second threshold used by
            # owned-worker supervision.  Ask the exact adopted process to stop,
            # then fence and terminate it before creating a replacement.  The
            # old implementation spawned immediately, so every replacement
            # failed on the still-held station lease and entered a restart loop.
            if adopted_pid > 0 and self._pid_is_alive(adopted_pid):
                stop_path = Path(
                    state.get("stop_path") or self._stop_path(station_id)
                )
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.touch(exist_ok=True)
                deadline = time.monotonic() + min(5.0, _STOP_TIMEOUT_SECONDS)
                while (
                    self._pid_is_alive(adopted_pid)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                if self._pid_is_alive(adopted_pid) and not self._terminate_pid(
                    adopted_pid
                ):
                    with self._lock:
                        state["last_error"] = "adopted_worker_termination_failed"
                    time.sleep(1.0)
                    continue
                stop_path.unlink(missing_ok=True)

            with self._lock:
                if not bool(state.get("desired_running")):
                    return
                state["adopted"] = False
                state["adopted_pid"] = None
                state["last_error"] = "adopted_worker_stalled"
            try:
                ready = self._spawn_generation(state)
                if not ready.wait(_STARTUP_TIMEOUT_SECONDS):
                    raise RuntimeError("replacement station worker did not become ready")
                self._monitor(state)
                return
            except Exception as exc:
                with self._lock:
                    state["last_error"] = f"adoption_recovery_{type(exc).__name__}"
                time.sleep(1.0)

    def _monitor(self, state: dict) -> None:
        while True:
            with self._lock:
                process = state.get("process")
                desired_running = bool(state.get("desired_running"))
                ready = bool(state.get("ready_event") and state["ready_event"].is_set())
                heartbeat_age = time.monotonic() - float(
                    state.get("last_heartbeat_monotonic") or time.monotonic()
                )
            if not desired_running or process is None:
                return

            if process.poll() is None and ready and heartbeat_age >= _HEARTBEAT_STALL_SECONDS:
                worker_pid = int(state.get("worker_pid") or process.pid)
                if not self._refresh_from_file_heartbeat(
                    state,
                    expected_pid=worker_pid,
                ):
                    with self._lock:
                        state["last_error"] = "heartbeat_stalled"
                        state["watchdog_terminations"] += 1
                    self._terminate_process(process)
            while process.poll() is None:
                with self._lock:
                    if not bool(state.get("desired_running")):
                        return
                    heartbeat_age = time.monotonic() - float(
                        state.get("last_heartbeat_monotonic") or time.monotonic()
                    )
                    ready = bool(
                        state.get("ready_event") and state["ready_event"].is_set()
                    )
                if ready and heartbeat_age >= _HEARTBEAT_STALL_SECONDS:
                    worker_pid = int(state.get("worker_pid") or process.pid)
                    if not self._refresh_from_file_heartbeat(
                        state,
                        expected_pid=worker_pid,
                    ):
                        with self._lock:
                            state["last_error"] = "heartbeat_stalled"
                            state["watchdog_terminations"] += 1
                        self._terminate_process(process)
                        break
                time.sleep(0.25)

            self._cleanup_generation(state)

            with self._lock:
                if not bool(state.get("desired_running")):
                    return
                now = time.monotonic()
                failures = [
                    value
                    for value in state.get("restart_failures", [])
                    if now - float(value) <= _RESTART_WINDOW_SECONDS
                ]
                failures.append(now)
                state["restart_failures"] = failures
                state["restart_count"] += 1
                restart_count = len(failures)

            if restart_count > _RESTART_BUDGET:
                open_until = time.monotonic() + _CIRCUIT_OPEN_SECONDS
                with self._lock:
                    state["circuit_state"] = "open"
                    state["circuit_open_until"] = open_until
                    state["last_error"] = "restart_budget_exhausted"
                while time.monotonic() < open_until:
                    with self._lock:
                        if not bool(state.get("desired_running")):
                            return
                    time.sleep(0.5)
                with self._lock:
                    state["restart_failures"] = []
                    state["circuit_state"] = "half_open"
            else:
                delay = _failure_backoff_seconds(restart_count, 0.1)
                deadline = time.monotonic() + delay
                with self._lock:
                    state["last_backoff_seconds"] = delay
                while time.monotonic() < deadline:
                    with self._lock:
                        if not bool(state.get("desired_running")):
                            return
                    time.sleep(0.1)

            try:
                self._spawn_generation(state)
            except Exception as exc:
                with self._lock:
                    state["last_error"] = f"spawn_{type(exc).__name__}"
                time.sleep(1.0)

    def start(
        self, station_id: int, fallback_uri: str = "", interval_sec: float = 1.0
    ) -> dict:
        station_id = int(station_id)
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

        with self._lock:
            existing = self._states.get(station_id)
            if existing and bool(existing.get("desired_running")):
                process = existing.get("process")
                if process is not None and process.poll() is None:
                    if (
                        str(existing.get("fallback_uri") or "")
                        == str(resolved_fallback_uri)
                        and float(existing.get("interval_sec") or 1.0) == safe_interval
                    ):
                        return self.status(station_id)

        if existing:
            stopped = self.stop(station_id)
            if bool(stopped.get("running")):
                raise RuntimeError(
                    "existing station worker did not stop; refusing duplicate source"
                )

        adopted = self._adopt_existing_state(
            station_id,
            fallback_uri=str(resolved_fallback_uri),
            interval_sec=safe_interval,
        )
        if adopted is not None:
            self._ensure_audio_bridge(adopted)
            monitor = threading.Thread(
                target=self._monitor_adopted,
                args=(adopted,),
                daemon=True,
                name=f"station-worker-adopted-monitor-{station_id}",
            )
            adopted["monitor_thread"] = monitor
            with self._lock:
                self._states[station_id] = adopted
            monitor.start()
            return self.status(station_id)

        state = {
            "audio_bridge_path": self._audio_bridge_path(station_id),
            "circuit_open_until": 0.0,
            "circuit_state": "closed",
            "desired_running": True,
            "failure_count": 0,
            "fallback_uri": str(resolved_fallback_uri),
            "generation": 0,
            "interval_sec": safe_interval,
            "last_backoff_seconds": 0.0,
            "last_error": "",
            "last_heartbeat_monotonic": time.monotonic(),
            "last_heartbeat_epoch": time.time(),
            "last_result": None,
            "last_runtime_status": None,
            "restart_count": 0,
            "restart_failures": [],
            "station_id": station_id,
            "ticks": 0,
            "watchdog_terminations": 0,
            "worker_pid": None,
        }
        with self._lock:
            self._states[station_id] = state
        try:
            ready_event = self._spawn_generation(state)
            monitor = threading.Thread(
                target=self._monitor,
                args=(state,),
                daemon=True,
                name=f"station-worker-monitor-{station_id}",
            )
            with self._lock:
                state["monitor_thread"] = monitor
            monitor.start()
            if not ready_event.wait(_STARTUP_TIMEOUT_SECONDS):
                raise RuntimeError("station worker process did not become ready")
        except Exception:
            self.stop(station_id)
            raise
        return self.status(station_id)

    def _issue_runtime_command(
        self,
        station_id: int,
        method: str,
        *args,
        timeout_seconds: float = 15.0,
        **kwargs,
    ):
        station_id = int(station_id)
        with self._lock:
            state = self._states.get(station_id)
        if state is None or not bool(self.status(station_id).get("running")):
            raise RuntimeError("isolated station worker is not running")
        command_path = Path(
            state.get("command_path") or self._command_path(station_id)
        )
        ack_path = Path(state.get("ack_path") or self._ack_path(station_id))
        command_lock = state.get("command_lock")
        if command_lock is None:
            command_lock = threading.Lock()
            state["command_lock"] = command_lock
        command_id = secrets.token_hex(16)
        command = {
            "args": list(args),
            "command_id": command_id,
            "created_epoch": time.time(),
            "generation": int(state.get("generation") or 1),
            "kwargs": dict(kwargs),
            "method": str(method),
            "schema_version": 1,
            "station_id": station_id,
        }
        with command_lock:
            ack_path.unlink(missing_ok=True)
            self._atomic_write_json(command_path, command)
            deadline = time.monotonic() + max(0.1, float(timeout_seconds))
            while time.monotonic() < deadline:
                acknowledgement = self._read_json(ack_path)
                if str(acknowledgement.get("command_id") or "") == command_id:
                    command_path.unlink(missing_ok=True)
                    ack_path.unlink(missing_ok=True)
                    if not bool(acknowledgement.get("ok")):
                        error_code = str(
                            acknowledgement.get("error") or "runtime_command_failed"
                        )
                        raise RuntimeError(error_code)
                    return acknowledgement.get("result")
                if not bool(self.status(station_id).get("running")):
                    break
                time.sleep(0.05)
            command_path.unlink(missing_ok=True)
            ack_path.unlink(missing_ok=True)
            raise RuntimeError("isolated station runtime command timed out")

    def start_runtime(self, station_id: int, input_uri: str, **kwargs) -> dict:
        station_id = int(station_id)
        if not bool(self.status(station_id).get("running")):
            self.start(station_id)
        result = self._issue_runtime_command(
            station_id,
            "start_station",
            station_id,
            str(input_uri),
            **kwargs,
        )
        return dict(result or {})

    def stop_runtime(self, station_id: int) -> dict:
        station_id = int(station_id)
        if not bool(self.status(station_id).get("running")):
            return dict(self.status(station_id).get("runtime_status") or {})
        result = self._issue_runtime_command(
            station_id,
            "stop_station",
            station_id,
        )
        return dict(result or {})

    def runtime_status(self, station_id: int) -> dict:
        station_id = int(station_id)
        worker = self.status(station_id)
        snapshot = worker.get("runtime_status")
        return dict(snapshot or {})

    def evaluate_runtime(self, station_id: int) -> dict:
        result = self._issue_runtime_command(
            int(station_id),
            "evaluate_station",
            int(station_id),
        )
        return dict(result or {})

    def recover_runtime(self, station_id: int, *, force: bool = False) -> dict:
        result = self._issue_runtime_command(
            int(station_id),
            "recover_station",
            int(station_id),
            force=bool(force),
        )
        return dict(result or {})

    def refresh_runtime_settings(self, station_id: int) -> dict:
        result = self._issue_runtime_command(
            int(station_id),
            "refresh_live_audio_settings",
            int(station_id),
        )
        return dict(result or {})

    def refresh_output_settings(self, station_id: int) -> dict:
        result = self._issue_runtime_command(
            int(station_id),
            "refresh_output_settings",
            int(station_id),
        )
        return dict(result or {})

    def promote_runtime_live_mix(self, station_id: int, *, force: bool = False) -> bool:
        result = self._issue_runtime_command(
            int(station_id),
            "promote_live_mix",
            int(station_id),
            force=bool(force),
        )
        return bool(result)

    def soundboard_play(self, station_id: int, item: dict) -> bool:
        result = self._issue_runtime_command(
            int(station_id),
            "soundboard_play",
            int(station_id),
            item=dict(item),
        )
        return bool(result)

    def soundboard_stop(self, station_id: int, item_id: int | None = None) -> bool:
        result = self._issue_runtime_command(
            int(station_id),
            "soundboard_stop",
            int(station_id),
            item_id=(int(item_id) if item_id is not None else None),
        )
        return bool(result)

    def stop(self, station_id: int) -> dict:
        station_id = int(station_id)
        with self._lock:
            state = self._states.get(station_id)
            if state is None:
                return self.status(station_id)
            state["desired_running"] = False
            stop_path = state.get("stop_path")
            process = state.get("process")
            adopted_pid = int(state.get("adopted_pid") or 0)
            monitor = state.get("monitor_thread")
        if stop_path:
            Path(stop_path).touch(exist_ok=True)
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
        elif adopted_pid > 0:
            deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
            while self._pid_is_alive(adopted_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._pid_is_alive(adopted_pid):
                if not self._terminate_pid(adopted_pid):
                    with self._lock:
                        state["last_error"] = "adopted_worker_termination_failed"
                    return self.status(station_id)
        audio_bridge_host = state.get("audio_bridge_host")
        if audio_bridge_host is not None:
            audio_bridge_host.close()
            state["audio_bridge_host"] = None
        self._cleanup_generation(state)
        heartbeat_path = state.get("heartbeat_path")
        if heartbeat_path:
            Path(heartbeat_path).unlink(missing_ok=True)
        audio_bridge_path = state.get("audio_bridge_path")
        if audio_bridge_path:
            try:
                Path(audio_bridge_path).unlink(missing_ok=True)
            except OSError:
                # A replaced backend or security scanner can retain a mapping
                # briefly on Windows. The versioned file is safe to reuse.
                pass
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        with self._lock:
            if self._states.get(station_id) is state:
                self._states.pop(station_id, None)
        return self.status(station_id)

    def status(self, station_id: int) -> dict:
        station_id = int(station_id)
        with self._lock:
            state = self._states.get(station_id)
            if state is None:
                return {
                    "circuit_state": "closed",
                    "adopted": False,
                    "failure_count": 0,
                    "fallback_uri": "",
                    "generation": 0,
                    "heartbeat_age_seconds": 0.0,
                    "interval_sec": None,
                    "last_backoff_seconds": 0.0,
                    "last_error": "",
                    "last_result": None,
                    "mode": "process",
                    "next_attempt_in_seconds": 0.0,
                    "pid": None,
                    "restart_count": 0,
                    "runtime_status": None,
                    "running": False,
                    "stalled": False,
                    "station_id": station_id,
                    "stopping": False,
                    "ticks": 0,
                    "watchdog_terminations": 0,
                }
            process = state.get("process")
            adopted_pid = int(state.get("adopted_pid") or 0)
            worker_pid = int(state.get("worker_pid") or 0)
            running = bool(
                (process is not None and process.poll() is None)
                or (adopted_pid > 0 and self._pid_is_alive(adopted_pid))
            )
            now = time.monotonic()
            heartbeat_epoch = float(state.get("last_heartbeat_epoch") or 0.0)
            heartbeat_age = (
                max(0.0, time.time() - heartbeat_epoch)
                if heartbeat_epoch
                else max(
                    0.0,
                    now - float(state.get("last_heartbeat_monotonic") or now),
                )
            )
            circuit_open_until = float(state.get("circuit_open_until") or 0.0)
            return {
                "circuit_state": str(state.get("circuit_state") or "closed"),
                "failure_count": int(state.get("failure_count") or 0),
                "fallback_uri": str(state.get("fallback_uri") or ""),
                "generation": int(state.get("generation") or 0),
                "heartbeat_age_seconds": round(heartbeat_age, 3),
                "interval_sec": float(state.get("interval_sec") or 1.0),
                "last_backoff_seconds": float(
                    state.get("last_backoff_seconds") or 0.0
                ),
                "last_error": str(state.get("last_error") or ""),
                "last_result": state.get("last_result"),
                "runtime_status": state.get("last_runtime_status"),
                "mode": "process",
                "next_attempt_in_seconds": max(0.0, circuit_open_until - now),
                "pid": (
                    worker_pid
                    if worker_pid > 0 and self._pid_is_alive(worker_pid)
                    else int(process.pid)
                    if process is not None and process.poll() is None
                    else (adopted_pid if running else None)
                ),
                "restart_count": int(state.get("restart_count") or 0),
                "running": running,
                "adopted": bool(state.get("adopted")),
                "stalled": bool(running and heartbeat_age >= _HEARTBEAT_STALL_SECONDS),
                "station_id": station_id,
                "stopping": not bool(state.get("desired_running")),
                "ticks": int(state.get("ticks") or 0),
                "watchdog_terminations": int(
                    state.get("watchdog_terminations") or 0
                ),
            }

    def snapshot(self) -> list[dict]:
        with self._lock:
            station_ids = sorted(self._states)
        return [self.status(station_id) for station_id in station_ids]

    def stop_all(self) -> dict:
        with self._lock:
            station_ids = sorted(self._states)
        stopped = 0
        for station_id in station_ids:
            if not bool(self.stop(station_id).get("running")):
                stopped += 1
        return {"stations": station_ids, "stopped": stopped}
