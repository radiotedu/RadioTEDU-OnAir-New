import os
import socket
import sqlite3
import subprocess
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_INTERNAL = ROOT / "_internal"
LIVE_ROOT = (
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    / "RadioTEDU"
    / "OnAir"
)
SHARED_CONFIG_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir"
JWT_SECRET_FILE = Path(os.environ.get("RADIOTEDU_JWT_SECRET_FILE", r"C:\Users\tedu\AppData\Local\RadioTEDU\OnAir\secrets\jwt-signing.key"))
STABLE_BACKEND_EXE = Path(
    os.environ.get("RADIOTEDU_STABLE_BACKEND_EXE", "")
).expanduser()
os.environ.setdefault("CLEANROOM_USER_CONFIG_ROOT", str(SHARED_CONFIG_ROOT))
os.environ.setdefault("CLEANROOM_JWT_SECRET_FILE", str(JWT_SECRET_FILE))
DB_PATH = LIVE_ROOT / "cleanroom.db"
LOG_PATH = LIVE_ROOT / "playout_guard.log"
LOCK_PATH = LIVE_ROOT / "playout_guard.lock"

BASE_URL = "http://127.0.0.1:8100"
STREAM_STATION_PRIORITY = (5, 2, 1, 7)
FALLBACK_URI = "silence://continuous"
WORKER_INTERVAL_SEC = 1.0
POLL_SEC = 5.0

API_TIMEOUT_SEC = 4.0
BACKEND_START_INTERVAL_SEC = 20.0
BACKEND_START_GRACE_SEC = 120.0
# Starting several station runtimes can occupy the API worker pool for well
# over 25 seconds while audio continues normally.  Avoid killing every stream
# during that bounded startup pressure; unreachable backends still use the
# shorter start/recovery path above.
BACKEND_STALL_GRACE_SEC = 300.0
LOOP_START_INTERVAL_SEC = 10.0
RUNTIME_TICK_INTERVAL_SEC = 5.0
RUNTIME_BAD_GRACE_SEC = 6.0
EMERGENCY_FALLBACK_GRACE_SEC = 20.0
EMERGENCY_FALLBACK_INTERVAL_SEC = 30.0
FALLBACK_CONTENT_RECOVERY_INTERVAL_SEC = 15.0
HEALTH_LOG_INTERVAL_SEC = 60.0
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

os.environ.setdefault("LOCALAPPDATA", str(LIVE_ROOT.parent))
os.environ.setdefault("CLEANROOM_DB_PATH", str(DB_PATH))
os.environ.setdefault("CLEANROOM_TOOLS_DIR", str(LIVE_ROOT / "tools"))

sys.path.insert(0, str(APP_INTERNAL))
import requests  # noqa: E402
from app.auth.jwt_handler import create_access_token  # noqa: E402
from app.services.backend_reload_control import (  # noqa: E402
    consume_due_reload_request,
    new_supervisor_token,
    publish_supervisor_capability,
)

_LOCK_HANDLE = None


class BackendRestartRequired(RuntimeError):
    """Raised when a live backend can answer HTTP but a playout worker is frozen."""


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size >= LOG_MAX_BYTES:
            oldest = LOG_PATH.with_name(f"{LOG_PATH.name}.{LOG_BACKUP_COUNT}")
            try:
                oldest.unlink()
            except FileNotFoundError:
                pass
            for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
                source = LOG_PATH.with_name(f"{LOG_PATH.name}.{index}")
                target = LOG_PATH.with_name(f"{LOG_PATH.name}.{index + 1}")
                if source.exists():
                    source.replace(target)
            LOG_PATH.replace(LOG_PATH.with_name(f"{LOG_PATH.name}.1"))
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def acquire_singleton_lock() -> bool:
    global _LOCK_HANDLE
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _LOCK_HANDLE = handle
    return True


def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(1, "admin")}


def api_request(method: str, path: str, payload: dict | None = None, *, timeout: float = API_TIMEOUT_SEC) -> dict:
    response = requests.request(
        method,
        BASE_URL + path,
        headers=auth_headers(),
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        return {"ok": True}
    return dict(data) if isinstance(data, dict) else {"data": data}


def api_get(path: str, *, timeout: float = API_TIMEOUT_SEC) -> dict:
    return api_request("GET", path, timeout=timeout)


def api_post(path: str, payload: dict | None = None, *, timeout: float = API_TIMEOUT_SEC) -> dict:
    return api_request("POST", path, payload or {}, timeout=timeout)


def _backend_tcp_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8100), timeout=0.5):
            pass
        return True
    except Exception:
        return False


def backend_probe_status() -> str:
    try:
        response = requests.get(BASE_URL + "/api/health/ready", timeout=1.5)
        if response.status_code == 200:
            return "healthy"
        return "unhealthy"
    except requests.Timeout:
        return "stalled" if _backend_tcp_listening() else "unreachable"
    except requests.RequestException:
        return "unreachable"


def backend_probe() -> bool:
    return backend_probe_status() == "healthy"


def _creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _backend_process_ids() -> list[int]:
    if sys.platform != "win32":
        return []
    command = (
        "$portPids = @(Get-NetTCPConnection -LocalPort 8100 -State Listen "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess); "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$portPids -contains $_.ProcessId -or "
        "(($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
        "$_.CommandLine -like '*uvicorn app.main:app*') } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        output = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", command],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=_creation_flags(),
        )
    except Exception:
        return []
    pids = []
    for line in output.splitlines():
        try:
            pids.append(int(line.strip()))
        except Exception:
            continue
    return pids


def stop_backend_processes() -> None:
    for pid in _backend_process_ids():
        if pid == os.getpid():
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=_creation_flags(),
            )
            log(f"backend-stop pid={pid}")
        except Exception as exc:
            log(f"backend-stop error pid={pid} error={exc}")


def _python_executable() -> str:
    if not bool(getattr(sys, "frozen", False)):
        return str(Path(sys.executable))
    return shutil.which("python.exe") or shutil.which("python") or "python.exe"


def start_backend() -> int | None:
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(LIVE_ROOT.parent)
    env["CLEANROOM_DB_PATH"] = str(LIVE_ROOT / "cleanroom.db")
    env["CLEANROOM_DATA_ROOT"] = str(LIVE_ROOT)
    env["CLEANROOM_TOOLS_DIR"] = str(LIVE_ROOT / "tools")
    env["CLEANROOM_USER_CONFIG_ROOT"] = str(SHARED_CONFIG_ROOT)
    env["CLEANROOM_JWT_SECRET_FILE"] = str(JWT_SECRET_FILE)
    # Keep recovery startups short so the guard can bring streams back even
    # when AI preload or prefetch would otherwise stall the backend.
    env.pop("CLEANROOM_FAST_STARTUP", None)
    env["CLEANROOM_SKIP_STARTUP_AI"] = "1"
    env["PYTHONPATH"] = str(APP_INTERNAL)
    env["PYTHONUTF8"] = "1"
    env["RADIOTEDU_WEBSOCKET_ENABLED"] = "0"
    tools_bin = LIVE_ROOT / "tools" / "bin"
    env["PATH"] = str(tools_bin) + os.pathsep + str(env.get("PATH", ""))

    # Prefer the packaged backend whenever it is present.  The old fallback
    # Python tree can survive an upgrade under %LocalAppData% and otherwise
    # shadows the current operator routes/static wall assets.
    backend_exe = ROOT / "RadioTEDU-OnAir-Backend.exe"
    if str(STABLE_BACKEND_EXE) and STABLE_BACKEND_EXE.is_file():
        cmd = [str(STABLE_BACKEND_EXE), "--lifespan", "on"]
        cwd = STABLE_BACKEND_EXE.parent
    elif backend_exe.is_file():
        cmd = [str(backend_exe), "--lifespan", "on"]
        cwd = ROOT
    elif (APP_INTERNAL / "app" / "main.py").is_file():
        cmd = [
            _python_executable(),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8100",
            "--ws",
            "none",
            "--lifespan",
            "on",
        ]
        cwd = APP_INTERNAL
    else:
        cmd = [str(backend_exe), "--lifespan", "on"]
        cwd = ROOT

    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creation_flags(),
    )
    pid = int(getattr(process, "pid", 0) or 0) or None
    log(f"backend-start command={Path(cmd[0]).name} pid={pid or ''}")
    return pid


def required_outputs_healthy(runtime: dict) -> bool:
    if not bool(runtime.get("running", False)):
        return False
    required = runtime.get("required_outputs") or {}
    branches = runtime.get("branch_health") or {}
    if not isinstance(required, dict) or not isinstance(branches, dict):
        return True
    required_branches = [str(name) for name, enabled in required.items() if bool(enabled)]
    if not required_branches:
        return True
    return all(bool(branches.get(name, False)) for name in required_branches)


def runtime_summary(runtime: dict) -> str:
    branches = runtime.get("branch_health") or {}
    required = runtime.get("required_outputs") or {}
    return (
        f"running={bool(runtime.get('running', False))} "
        f"backend={runtime.get('backend', 'none')} "
        f"branches={branches} required={required}"
    )


def row_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def station_autostart_enabled(station_id: int) -> bool:
    """Return the operator's explicit per-station restart authorization."""
    try:
        conn = db_connect()
        try:
            row = conn.execute(
                """
                SELECT value
                FROM station_settings
                WHERE station_id=? AND key='broadcast_autostart_enabled'
                LIMIT 1
                """,
                (int(station_id),),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    token = str(row["value"] if row is not None else "false").strip().lower()
    return token in {"1", "true", "yes", "on"}


def supervised_station_ids() -> tuple[int, ...]:
    try:
        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT o.station_id
                FROM station_outputs o
                JOIN station_settings s
                  ON s.station_id=o.station_id
                 AND s.key='broadcast_autostart_enabled'
                WHERE o.icecast_enabled=1
                  AND lower(trim(s.value)) IN ('1', 'true', 'yes', 'on')
                ORDER BY o.station_id
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return ()
    station_ids = [int(row["station_id"]) for row in rows]
    priority = {sid: idx for idx, sid in enumerate(STREAM_STATION_PRIORITY)}
    return tuple(sorted(station_ids, key=lambda sid: (priority.get(int(sid), 1000), int(sid))))


def default_station_state() -> dict:
    return {
        "last_loop_start": 0.0,
        "last_runtime_tick": 0.0,
        "last_emergency_fallback": 0.0,
        "last_fallback_content_recovery": 0.0,
        "loop_bad_since": None,
        "runtime_bad_since": None,
        "last_summary": "",
        "last_health_log": 0.0,
    }


def playout_state(conn: sqlite3.Connection, station_id: int) -> dict:
    row = conn.execute(
        """
        SELECT current_source, current_item_id
        FROM playout_state
        WHERE station_id=?
        LIMIT 1
        """,
        (int(station_id),),
    ).fetchone()
    return row_dict(row) or {"current_source": "none", "current_item_id": None}


def current_playing_item(conn: sqlite3.Connection, station_id: int) -> dict | None:
    for source, table in (("ads", "ad_break_items"), ("manual", "queue_items")):
        row = conn.execute(
            f"""
            SELECT i.id AS item_id, i.station_id, i.track_id, i.started_at,
                   COALESCE(t.title, '') AS title,
                   COALESCE(t.artist, '') AS artist,
                   COALESCE(t.file_path, '') AS file_path,
                   COALESCE(t.track_type, 'music') AS track_type
            FROM {table} i
            LEFT JOIN tracks t ON t.id = i.track_id
            WHERE i.station_id=? AND i.status='playing'
            ORDER BY datetime(i.started_at) DESC, i.id DESC
            LIMIT 1
            """,
            (int(station_id),),
        ).fetchone()
        if row is not None:
            item = dict(row)
            item["source"] = source
            item["table"] = table
            return item
    return None


def sync_playout_state(conn: sqlite3.Connection, station_id: int, source: str, item_id: int) -> None:
    conn.execute(
        """
        INSERT INTO playout_state (station_id, current_source, current_item_id, started_at, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(station_id) DO UPDATE SET
            current_source=excluded.current_source,
            current_item_id=excluded.current_item_id,
            started_at=excluded.started_at,
            updated_at=CURRENT_TIMESTAMP
        """,
        (int(station_id), str(source), int(item_id)),
    )


def refresh_playing_started_at(conn: sqlite3.Connection, item: dict) -> None:
    conn.execute(
        f"UPDATE {item['table']} SET started_at=CURRENT_TIMESTAMP WHERE id=?",
        (int(item["item_id"]),),
    )
    sync_playout_state(
        conn,
        int(item["station_id"]),
        str(item["source"]),
        int(item["item_id"]),
    )
    conn.commit()


def play_item(station_id: int, item: dict) -> bool:
    file_path = str(item.get("file_path") or "").strip()
    if not file_path:
        return False
    if "://" not in file_path and not Path(file_path).is_file():
        return False
    api_post(
        f"/api/runtime/{int(station_id)}/start",
        {
            "input_uri": file_path,
            "stream_title": str(item.get("title") or ""),
            "stream_artist": str(item.get("artist") or ""),
        },
        timeout=8.0,
    )
    return True


def looks_like_continuity(loop_payload: dict, runtime: dict, db_state: dict) -> bool:
    source = str(db_state.get("current_source") or "").strip().lower()
    if source == "fallback":
        return True
    active_uri = str(runtime.get("active_input_uri") or "").strip().lower()
    if active_uri.startswith("silence://"):
        return True
    active_title = str(runtime.get("active_stream_title") or "").strip().lower()
    if "continuity" in active_title:
        return True
    result = loop_payload.get("last_result")
    if isinstance(result, dict):
        result_source = str(result.get("source") or "").strip().lower()
        result_uri = str(result.get("input_uri") or "").strip().lower()
        if result_source == "fallback" and result_uri.startswith("silence://") and source in {"fallback", "none"}:
            return True
    return False


def recover_content_from_fallback(station_id: int, station_state: dict, loop_payload: dict, runtime: dict) -> bool:
    now = time.monotonic()
    if now - float(station_state.get("last_fallback_content_recovery") or 0.0) < FALLBACK_CONTENT_RECOVERY_INTERVAL_SEC:
        return False

    conn = db_connect()
    try:
        db_state = playout_state(conn, station_id)
        if not looks_like_continuity(loop_payload, runtime, db_state):
            return False
        item = current_playing_item(conn, station_id)
        if item is None:
            return False
        if not play_item(station_id, item):
            return False
        refresh_playing_started_at(conn, item)
    finally:
        conn.close()

    station_state["last_fallback_content_recovery"] = now
    log(
        "fallback-content-recovered station={station} source={source} item={item} track={track} title={title}".format(
            station=station_id,
            source=str(item.get("source") or ""),
            item=int(item.get("item_id") or 0),
            track=int(item.get("track_id") or 0),
            title=repr(str(item.get("title") or "")),
        )
    )
    return True


def station_health_summary(loop_payload: dict, runtime: dict) -> str:
    return (
        f"loop={bool(loop_payload.get('running', False))} "
        f"{runtime_summary(runtime)}"
    )


def ensure_backend(state: dict) -> bool:
    status = backend_probe_status()
    if status == "healthy":
        state["backend_bad_since"] = None
        state["backend_starting_since"] = None
        state["backend_last_status"] = status
        return True

    now = time.monotonic()
    if state.get("backend_bad_since") is None:
        state["backend_bad_since"] = now
        log(f"backend-{status}")
    elif state.get("backend_last_status") != status:
        log(f"backend-{status}")
    state["backend_last_status"] = status
    starting_since = state.get("backend_starting_since")
    bad_for = now - float(state.get("backend_bad_since") or now)
    if status != "stalled" and starting_since is not None and now - float(starting_since or now) < BACKEND_START_GRACE_SEC:
        return False
    if status == "stalled" and bad_for < BACKEND_STALL_GRACE_SEC:
        return False
    if now - float(state.get("last_backend_start") or 0.0) >= BACKEND_START_INTERVAL_SEC:
        state["last_backend_start"] = now
        state["backend_starting_since"] = now
        try:
            if status == "stalled":
                stop_backend_processes()
            state["backend_pid"] = start_backend()
        except Exception as exc:
            log(f"backend-start error={exc}")
    return False


def process_supervised_reload_request(state: dict, supervisor_token: str) -> bool:
    request = consume_due_reload_request(supervisor_token)
    if request is None:
        return False
    request_id = str(request.get("request_id") or "unknown")
    previous_instance = str(request.get("backend_instance_id") or "unknown")
    log(
        "backend-reload accepted "
        f"request_id={request_id} previous_instance={previous_instance}"
    )
    stop_backend_processes()
    now = time.monotonic()
    state["last_backend_start"] = now
    state["backend_starting_since"] = now
    state["backend_bad_since"] = None
    state["backend_last_status"] = "reloading"
    state["backend_pid"] = start_backend()
    log(
        "backend-reload started "
        f"request_id={request_id} pid={state['backend_pid']}"
    )
    return True


def ensure_worker_loop(station_id: int, station_state: dict, loop_payload: dict) -> dict:
    if bool(loop_payload.get("running", False)):
        station_state["loop_bad_since"] = None
        return loop_payload

    now = time.monotonic()
    if station_state.get("loop_bad_since") is None:
        station_state["loop_bad_since"] = now
        log(f"loop-stopped station={station_id}")

    if now - float(station_state.get("last_loop_start") or 0.0) < LOOP_START_INTERVAL_SEC:
        return loop_payload

    station_state["last_loop_start"] = now
    result = api_post(
        f"/api/runtime/{int(station_id)}/loop/start",
        {"fallback_uri": FALLBACK_URI, "interval_sec": WORKER_INTERVAL_SEC},
    )
    log(
        "loop-start station={station} running={running} ticks={ticks}".format(
            station=station_id,
            running=bool(result.get("running", False)),
            ticks=int(result.get("ticks") or 0),
        )
    )
    return result


def tick_worker(station_id: int, station_state: dict, reason: str) -> None:
    now = time.monotonic()
    if now - float(station_state.get("last_runtime_tick") or 0.0) < RUNTIME_TICK_INTERVAL_SEC:
        return
    station_state["last_runtime_tick"] = now
    result = api_post(
        f"/api/runtime/{int(station_id)}/tick",
        {"fallback_uri": FALLBACK_URI},
        timeout=8.0,
    )
    log(f"runtime-tick station={station_id} reason={reason} result={result}")


def emergency_fallback(station_id: int, station_state: dict, reason: str) -> None:
    now = time.monotonic()
    if now - float(station_state.get("last_emergency_fallback") or 0.0) < EMERGENCY_FALLBACK_INTERVAL_SEC:
        return
    station_state["last_emergency_fallback"] = now
    api_post(
        f"/api/runtime/{int(station_id)}/start",
        {
            "input_uri": FALLBACK_URI,
            "stream_title": f"Station {int(station_id)} Continuity",
            "stream_artist": "",
        },
        timeout=8.0,
    )
    log(f"emergency-fallback station={station_id} reason={reason}")


def supervise_station(station_id: int, station_state: dict) -> None:
    if not station_autostart_enabled(station_id):
        station_state["loop_bad_since"] = None
        station_state["runtime_bad_since"] = None
        return
    loop_payload = api_get(f"/api/runtime/{int(station_id)}/loop/status")
    loop_payload = ensure_worker_loop(station_id, station_state, loop_payload)
    if bool(loop_payload.get("stalled", False)):
        elapsed = float(loop_payload.get("tick_elapsed_seconds") or 0.0)
        raise BackendRestartRequired(
            f"station={int(station_id)} worker_tick_stalled_seconds={elapsed:.1f}"
        )
    runtime = loop_payload.get("runtime")
    if not isinstance(runtime, dict):
        runtime = api_get(f"/api/runtime/{int(station_id)}/status")

    now = time.monotonic()
    summary = station_health_summary(loop_payload, runtime)
    last_summary = str(station_state.get("last_summary") or "")
    last_health_log = float(station_state.get("last_health_log") or 0.0)
    if summary != last_summary or now - last_health_log >= HEALTH_LOG_INTERVAL_SEC:
        log(
            "health station={station} ticks={ticks} {summary}".format(
                station=station_id,
                ticks=int(loop_payload.get("ticks") or 0),
                summary=summary,
            )
        )
        station_state["last_summary"] = summary
        station_state["last_health_log"] = now

    if required_outputs_healthy(runtime):
        station_state["runtime_bad_since"] = None
        return

    if station_state.get("runtime_bad_since") is None:
        station_state["runtime_bad_since"] = now
        log(f"runtime-unhealthy station={station_id} {runtime_summary(runtime)}")

    bad_for = now - float(station_state.get("runtime_bad_since") or now)
    loop_running = bool(loop_payload.get("running", False))
    runtime_running = bool(runtime.get("running", False))
    if bad_for >= RUNTIME_BAD_GRACE_SEC and not loop_running:
        tick_worker(station_id, station_state, "runtime_unhealthy")
    if bad_for >= EMERGENCY_FALLBACK_GRACE_SEC and not loop_running and not runtime_running:
        emergency_fallback(station_id, station_state, "runtime_unhealthy")


def main() -> int:
    if not acquire_singleton_lock():
        log("playout guard already running; duplicate exiting")
        return 0
    log("playout guard starting supervisor mode")
    supervisor_token = new_supervisor_token()
    state = {
        "last_backend_start": 0.0,
        "backend_bad_since": None,
        "backend_last_status": "",
        "backend_pid": None,
        "stations": {},
    }

    while True:
        try:
            publish_supervisor_capability(
                supervisor_token,
                supervisor_pid=os.getpid(),
            )
            if process_supervised_reload_request(state, supervisor_token):
                time.sleep(POLL_SEC)
                continue
            if ensure_backend(state):
                for station_id in supervised_station_ids():
                    station_state = state["stations"].setdefault(int(station_id), default_station_state())
                    try:
                        supervise_station(int(station_id), station_state)
                    except BackendRestartRequired as exc:
                        # A wedged Python thread cannot be killed safely in
                        # process.  The external supervisor is the containment
                        # boundary: restart the backend, then startup reconcile
                        # restores the playing row to the front of its queue.
                        log(f"backend-restart-required reason={exc}")
                        stop_backend_processes()
                        now = time.monotonic()
                        state["last_backend_start"] = now
                        state["backend_starting_since"] = now
                        state["backend_bad_since"] = None
                        state["backend_last_status"] = "worker-stalled"
                        state["stations"] = {}
                        try:
                            state["backend_pid"] = start_backend()
                            log(
                                "backend-restarted-after-worker-stall "
                                f"pid={state['backend_pid']}"
                            )
                        except Exception as start_exc:
                            log(
                                "backend-restart-after-worker-stall-failed "
                                f"error={start_exc}"
                            )
                        break
                    except Exception as exc:
                        log(f"station-cycle error station={station_id} error={exc}")
        except Exception as exc:
            log(f"cycle error={exc}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
