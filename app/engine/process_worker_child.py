from __future__ import annotations

import base64
import faulthandler
import json
import logging
import os
import secrets
import threading
import time
from logging.handlers import RotatingFileHandler
from multiprocessing.connection import Client
from pathlib import Path

from app.db import init_db
from app.engine.process_audio_bridge import ProcessAudioBridgeClient
from app.engine.runtime_registry import StationRuntimeRegistry
from app.engine.runtime_supervisor import RuntimeSupervisor
from app.engine.station_worker import StationWorker
from app.engine.worker_loop import _failure_backoff_seconds


class RemoteRuntimeRegistry:
    def __init__(self, *, address: str, family: str, authkey: bytes, station_id: int):
        self.station_id = int(station_id)
        self._connection = Client(address, family=family, authkey=authkey)

    def _request(self, method: str, *args, **kwargs):
        self._connection.send(
            {"args": list(args), "kwargs": dict(kwargs), "method": str(method)}
        )
        response = dict(self._connection.recv() or {})
        if not bool(response.get("ok")):
            raise RuntimeError(str(response.get("error") or "station runtime RPC failed"))
        return response.get("result")

    def start_station(self, station_id: int, input_uri: str, **kwargs) -> dict:
        return dict(
            self._request("start_station", int(station_id), str(input_uri), **kwargs)
            or {}
        )

    def stop_station(self, station_id: int) -> dict:
        return dict(self._request("stop_station", int(station_id)) or {})

    def status(self, station_id: int) -> dict:
        return dict(self._request("status", int(station_id)) or {})

    def is_process_running(self, station_id: int) -> bool:
        return bool(self._request("is_process_running", int(station_id)))

    def evaluate_station(self, station_id: int) -> dict:
        return dict(self._request("evaluate_station", int(station_id)) or {})

    def heartbeat(self, payload: dict) -> None:
        self._request("heartbeat", self.station_id, dict(payload))

    def close(self) -> None:
        self._connection.close()


def _load_config() -> dict:
    config_path = Path(
        os.environ.get("RADIOTEDU_STATION_WORKER_CONFIG", "")
    ).expanduser().resolve()
    if not config_path.is_file():
        raise RuntimeError("station worker configuration is missing")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) not in {1, 2}:
        raise RuntimeError("station worker configuration version is unsupported")
    return dict(payload)


def _configure_environment(config: dict) -> None:
    mappings = {
        "CLEANROOM_DATA_ROOT": "data_root",
        "CLEANROOM_DB_PATH": "database_path",
        "CLEANROOM_USER_CONFIG_ROOT": "user_config_root",
    }
    for environment_name, config_name in mappings.items():
        value = str(config.get(config_name) or "").strip()
        if value:
            os.environ[environment_name] = value
    os.environ["CLEANROOM_OPEN_PANEL"] = "0"
    os.environ["RADIOTEDU_PROCESS_ISOLATED_WORKERS"] = "0"


def _configure_logging(config: dict, station_id: int) -> logging.Logger:
    logger = logging.getLogger(f"cleanroom.station_process.{station_id}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path = Path(str(config["log_path"])).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s station=%(name)s %(message)s")
    )
    logger.addHandler(handler)
    return logger


def _stop_requested(stop_path: Path) -> bool:
    return stop_path.exists()


class StationProcessLease:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._file = handle
        return True

    def close(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


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
        # Antivirus, search indexing, or a concurrent reader can briefly hold
        # the destination without delete sharing on Windows.  Keep liveness
        # observable through that race instead of dropping the heartbeat.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except OSError:
                if attempt >= 5:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _write_heartbeat(
    config: dict,
    payload: dict,
    *,
    runtime_status: dict | None,
    running: bool,
) -> None:
    configured = str(config.get("heartbeat_path") or "").strip()
    if not configured:
        return
    heartbeat_path = Path(configured).expanduser().resolve()
    document = {
        "failure_count": int(payload.get("failure_count") or 0),
        "fallback_uri": str(config.get("fallback_uri") or ""),
        "generation": int(config.get("generation") or 1),
        "interval_sec": float(config.get("interval_sec") or 1.0),
        "last_backoff_seconds": float(
            payload.get("last_backoff_seconds") or 0.0
        ),
        "last_error": str(payload.get("last_error") or "")[:120],
        "last_result": payload.get("last_result"),
        "pid": os.getpid(),
        "running": bool(running),
        "runtime_status": dict(runtime_status or {}),
        "scheduler_progress_epoch": float(
            payload.get("scheduler_progress_epoch") or time.time()
        ),
        "scheduler_stalled": bool(payload.get("scheduler_stalled", False)),
        "scheduler_tick_age_seconds": float(
            payload.get("scheduler_tick_age_seconds") or 0.0
        ),
        "transport_healthy": bool(
            payload.get("transport_healthy", _transport_is_healthy(runtime_status))
        ),
        "schema_version": 1,
        "station_id": int(config["station_id"]),
        "ticks": int(payload.get("ticks") or 0),
        "updated_epoch": time.time(),
        "worker_id": str(config.get("worker_id") or ""),
    }
    _atomic_write_json(heartbeat_path, document)


def _transport_is_healthy(runtime_status: dict | None) -> bool:
    """Return whether the worker is still delivering current program audio."""

    status = dict(runtime_status or {})
    if not bool(status.get("program_running")):
        return False
    if bool(status.get("program_pcm_stalled")):
        return False
    try:
        pcm_age = float(status.get("program_pcm_age_seconds") or 0.0)
    except (TypeError, ValueError):
        return False
    if pcm_age > 5.0:
        return False

    required = dict(status.get("required_outputs") or {})
    if bool(required.get("icecast")):
        mount = dict(status.get("icecast_mount_health") or {})
        try:
            write_age = float(mount.get("last_write_age_seconds") or 0.0)
        except (TypeError, ValueError):
            return False
        if (
            not bool(mount.get("process_running"))
            or not bool(mount.get("writer_running"))
            or bool(mount.get("writer_failed"))
            or bool(mount.get("writer_backpressured"))
            or write_age > 5.0
        ):
            return False
    if bool(required.get("local")) and not bool(status.get("local_sink_running")):
        return False
    return True


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _process_runtime_command(
    config: dict,
    runtime_registry: StationRuntimeRegistry,
    runtime_supervisor: RuntimeSupervisor,
    last_command_id: str,
) -> str:
    configured_command = str(config.get("command_path") or "").strip()
    configured_ack = str(config.get("ack_path") or "").strip()
    if not configured_command or not configured_ack:
        return last_command_id
    command_path = Path(configured_command).expanduser().resolve()
    acknowledgement_path = Path(configured_ack).expanduser().resolve()
    command = _read_json(command_path)
    command_id = str(command.get("command_id") or "")
    if not command_id or command_id == last_command_id:
        return last_command_id

    station_id = int(config["station_id"])
    method = str(command.get("method") or "")
    args = list(command.get("args") or [])
    kwargs = dict(command.get("kwargs") or {})
    acknowledgement = {
        "command_id": command_id,
        "completed_epoch": time.time(),
        "ok": False,
        "result": None,
        "schema_version": 1,
        "station_id": station_id,
    }
    try:
        if int(command.get("station_id") or 0) != station_id:
            raise RuntimeError("station_boundary_rejected")
        if int(command.get("generation") or 0) != int(config.get("generation") or 1):
            raise RuntimeError("stale_generation_rejected")
        if not args or int(args[0]) != station_id:
            raise RuntimeError("station_argument_rejected")
        if method == "start_station":
            result = runtime_registry.start_station(*args, **kwargs)
        elif method == "stop_station":
            result = runtime_registry.stop_station(station_id)
        elif method == "status":
            result = runtime_registry.status(station_id)
        elif method == "is_process_running":
            result = runtime_registry.is_process_running(station_id)
        elif method == "evaluate_station":
            result = runtime_supervisor.evaluate_station(station_id)
        elif method == "recover_station":
            result = runtime_registry.recover_station(station_id, **kwargs)
        elif method == "refresh_live_audio_settings":
            result = runtime_registry.refresh_live_audio_settings(station_id)
        elif method == "refresh_output_settings":
            result = runtime_registry.refresh_output_settings(station_id)
        elif method == "promote_live_mix":
            result = runtime_registry.promote_live_mix(
                station_id,
                force=bool(kwargs.get("force", False)),
            )
        elif method == "soundboard_play":
            player = runtime_registry.get_sound_effect_player(station_id)
            if player is None:
                raise RuntimeError("station_runtime_not_running")
            player.play(dict(kwargs.get("item") or {}))
            runtime_registry.promote_live_mix(station_id, force=True)
            result = True
        elif method == "soundboard_stop":
            player = runtime_registry.get_sound_effect_player(station_id)
            if player is None:
                raise RuntimeError("station_runtime_not_running")
            item_id = kwargs.get("item_id")
            player.stop(item_id=int(item_id) if item_id is not None else None)
            result = True
        else:
            raise RuntimeError("runtime_command_not_allowed")
        acknowledgement["ok"] = True
        acknowledgement["result"] = result
    except Exception as exc:
        acknowledgement["error"] = f"RUNTIME_{type(exc).__name__.upper()}"
    _atomic_write_json(acknowledgement_path, acknowledgement)
    return command_id


def run_station_worker_process() -> int:
    config = _load_config()
    _configure_environment(config)
    station_id = int(config["station_id"])
    interval_sec = max(0.1, float(config.get("interval_sec") or 1.0))
    fallback_uri = str(config.get("fallback_uri") or "")
    stop_path = Path(str(config["stop_path"])).expanduser().resolve()
    logger = _configure_logging(config, station_id)
    authkey = base64.b64decode(str(config["rpc_authkey"]).encode("ascii"))
    lock_path = Path(
        str(config.get("lock_path") or stop_path.with_suffix(".lease"))
    ).expanduser().resolve()
    lease = StationProcessLease(lock_path)
    if not lease.acquire():
        logger.error("station lease already held; refusing duplicate source owner")
        return 73

    fault_dump_seconds = 0.0
    try:
        fault_dump_seconds = float(
            os.getenv("RADIOTEDU_WORKER_FAULT_DUMP_SECONDS", "0") or 0
        )
    except (TypeError, ValueError):
        fault_dump_seconds = 0.0
    if fault_dump_seconds > 0:
        faulthandler.enable()
        faulthandler.dump_traceback_later(
            max(1.0, fault_dump_seconds),
            repeat=True,
        )

    init_db()
    remote = None
    autonomous = os.getenv(
        "RADIOTEDU_STATION_WORKER_AUTONOMOUS", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if autonomous:
        logger.warning("autonomous recovery mode enabled; backend RPC skipped")
    else:
        try:
            remote = RemoteRuntimeRegistry(
                address=str(config["rpc_address"]),
                family=str(config["rpc_family"]),
                authkey=authkey,
                station_id=station_id,
            )
        except (OSError, EOFError):
            logger.warning("backend control channel unavailable; entering autonomous mode")
    audio_bridge = None
    audio_bridge_path = str(config.get("audio_bridge_path") or "").strip()
    if audio_bridge_path:
        try:
            audio_bridge = ProcessAudioBridgeClient(Path(audio_bridge_path))
        except (OSError, RuntimeError):
            logger.warning(
                "station audio bridge unavailable; live inputs start muted"
            )
    if audio_bridge is None:
        runtime_registry = StationRuntimeRegistry()
    else:
        runtime_registry = StationRuntimeRegistry(
            live_mic_registry=audio_bridge,
            guest_audio_registry=audio_bridge,
        )
    runtime_supervisor = RuntimeSupervisor(runtime_registry)
    worker_id = str(config["worker_id"])
    ticks = 0
    failure_count = 0
    last_command_id = ""
    last_heartbeat_error_log = 0.0
    last_file_error_log = 0.0
    heartbeat_write_lock = threading.Lock()
    heartbeat_state_lock = threading.Lock()
    heartbeat_stop = threading.Event()
    heartbeat_state = {
        "payload": {
            "event": "ready",
            "failure_count": 0,
            "last_error": "",
            "last_result": None,
            "ticks": 0,
        },
        "runtime_status": runtime_registry.status(station_id),
        "scheduler_progress_epoch": time.time(),
    }

    def emit(payload: dict, runtime_status: dict, *, running: bool = True) -> None:
        nonlocal remote, last_heartbeat_error_log, last_file_error_log
        safe_payload = dict(payload)
        # On Windows, a virtual-environment launcher can remain as a wrapper
        # around the real interpreter.  Report the worker PID explicitly so
        # the backend exposes and later adopts the process that owns the
        # station lease, rather than the wrapper returned by subprocess.Popen.
        safe_payload["pid"] = os.getpid()
        safe_payload["runtime_status"] = dict(runtime_status or {})
        with heartbeat_state_lock:
            heartbeat_state["payload"] = dict(payload)
            heartbeat_state["runtime_status"] = dict(runtime_status or {})
            heartbeat_state["scheduler_progress_epoch"] = time.time()
        try:
            with heartbeat_write_lock:
                _write_heartbeat(
                    config,
                    safe_payload,
                    runtime_status=runtime_status,
                    running=running,
                )
        except OSError:
            now = time.monotonic()
            if now - last_file_error_log >= 60.0:
                logger.warning("atomic station heartbeat write failed")
                last_file_error_log = now
        if remote is None:
            return
        try:
            remote.heartbeat(safe_payload)
        except (OSError, EOFError, RuntimeError):
            now = time.monotonic()
            if now - last_heartbeat_error_log >= 60.0:
                logger.warning(
                    "backend control channel lost; broadcast remains autonomous"
                )
                last_heartbeat_error_log = now
            try:
                remote.close()
            except Exception:
                pass
            remote = None

    def emit_liveness() -> None:
        """Refresh file health while scheduler work is still in progress."""

        while not heartbeat_stop.wait(1.0):
            with heartbeat_state_lock:
                payload = dict(heartbeat_state["payload"])
                last_status = dict(heartbeat_state["runtime_status"])
                progress_epoch = float(heartbeat_state["scheduler_progress_epoch"])
            try:
                current_status = runtime_registry.status(station_id)
            except Exception:
                current_status = last_status
            scheduler_age = max(0.0, time.time() - progress_epoch)
            payload["event"] = "liveness"
            payload["scheduler_progress_epoch"] = progress_epoch
            payload["scheduler_tick_age_seconds"] = round(scheduler_age, 3)
            payload["scheduler_stalled"] = scheduler_age >= 60.0
            payload["transport_healthy"] = _transport_is_healthy(current_status)
            try:
                with heartbeat_write_lock:
                    _write_heartbeat(
                        config,
                        payload,
                        runtime_status=current_status,
                        running=True,
                    )
            except OSError:
                # A short antivirus/filesystem race must not stop liveness.
                pass

    liveness_thread = threading.Thread(
        target=emit_liveness,
        daemon=True,
        name=f"station-worker-liveness-{station_id}",
    )

    try:
        emit(
            {
                "event": "ready",
                "failure_count": 0,
                "last_error": "",
                "last_result": None,
                "ticks": 0,
            },
            runtime_registry.status(station_id),
        )
        liveness_thread.start()
        while not _stop_requested(stop_path):
            started = time.monotonic()
            worker = None
            result = None
            error_code = ""
            backoff_seconds = 0.0
            try:
                last_command_id = _process_runtime_command(
                    config,
                    runtime_registry,
                    runtime_supervisor,
                    last_command_id,
                )
                supervisor = runtime_supervisor.evaluate_station(station_id)
                if (
                    not bool(supervisor.get("running", True))
                    and str(supervisor.get("action") or "") == "restart_last_resort"
                    and fallback_uri
                ):
                    runtime_registry.start_station(
                        station_id,
                        fallback_uri,
                        stream_title="Continuity audio",
                        track_type="startup",
                    )
                worker = StationWorker(
                    station_id=station_id,
                    worker_id=worker_id,
                    runtime_registry=runtime_registry,
                    fallback_uri=fallback_uri,
                )
                result = worker.process_once()
                failure_count = 0
            except Exception as exc:
                failure_count += 1
                error_code = type(exc).__name__
                backoff_seconds = _failure_backoff_seconds(
                    failure_count, interval_sec
                )
                logger.warning(
                    "worker tick failed code=%s failure_count=%d",
                    error_code,
                    failure_count,
                )
            finally:
                if worker is not None:
                    worker.conn.close()

            ticks += 1
            runtime_status = runtime_registry.status(station_id)
            emit(
                {
                    "event": "tick",
                    "failure_count": failure_count,
                    "last_backoff_seconds": backoff_seconds,
                    "last_error": error_code,
                    "last_result": result,
                    "tick_duration_seconds": round(time.monotonic() - started, 3),
                    "ticks": ticks,
                },
                runtime_status,
            )
            deadline = time.monotonic() + max(interval_sec, backoff_seconds)
            runtime_was_running = runtime_registry.is_process_running(station_id)
            while not _stop_requested(stop_path):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if runtime_was_running and not runtime_registry.is_process_running(station_id):
                    break
                last_command_id = _process_runtime_command(
                    config,
                    runtime_registry,
                    runtime_supervisor,
                    last_command_id,
                )
                time.sleep(min(0.1, remaining))
    finally:
        if fault_dump_seconds > 0:
            faulthandler.cancel_dump_traceback_later()
        heartbeat_stop.set()
        if liveness_thread.is_alive():
            liveness_thread.join(timeout=2.0)
        try:
            runtime_registry.stop_all()
        except Exception:
            logger.warning("runtime stop failed during worker shutdown")
        emit(
            {
                "event": "stopped",
                "failure_count": failure_count,
                "last_error": "",
                "last_result": None,
                "ticks": ticks,
            },
            runtime_registry.status(station_id),
            running=False,
        )
        if remote is not None:
            remote.close()
        if audio_bridge is not None:
            audio_bridge.close()
        lease.close()
    return 0
