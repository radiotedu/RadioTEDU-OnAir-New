import os
import threading
import time

from app.db import get_connection, init_db
from app.engine.process_worker_manager import ProcessIsolatedStationWorkerManager
from app.repositories.station_repo import StationRepository


class _RuntimeRegistryStub:
    def __init__(self):
        self._lock = threading.Lock()
        self._running = {}

    def start_station(self, station_id, input_uri, **kwargs):
        with self._lock:
            self._running[int(station_id)] = True
        return {
            "active_input_uri": str(input_uri),
            "running": True,
            "station_id": int(station_id),
            "track_type": str(kwargs.get("track_type") or "music"),
        }

    def stop_station(self, station_id):
        with self._lock:
            self._running[int(station_id)] = False
        return {"running": False, "station_id": int(station_id)}

    def status(self, station_id):
        with self._lock:
            running = bool(self._running.get(int(station_id), False))
        return {
            "active_input_uri": "",
            "running": running,
            "station_id": int(station_id),
            "track_type": "",
        }

    def is_process_running(self, station_id):
        with self._lock:
            return bool(self._running.get(int(station_id), False))


def _wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_station_schedulers_run_in_independent_restartable_processes(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "data"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(data_root / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(tmp_path / "user"))
    monkeypatch.setenv("CLEANROOM_OPEN_PANEL", "0")
    monkeypatch.delenv("RTAI_ONAIR_PRODUCT", raising=False)
    init_db()

    conn = get_connection()
    try:
        first_station_id = int(
            conn.execute("SELECT MIN(id) FROM stations").fetchone()[0]
        )
        second_station_id = StationRepository(conn).create("Process Two")
    finally:
        conn.close()

    registry = _RuntimeRegistryStub()
    manager = ProcessIsolatedStationWorkerManager(registry)
    try:
        first = manager.start(first_station_id, interval_sec=0.2)
        second = manager.start(second_station_id, interval_sec=0.2)
        assert first["mode"] == "process"
        assert second["mode"] == "process"
        assert first["pid"] not in {None, os.getpid()}
        assert second["pid"] not in {None, os.getpid(), first["pid"]}
        assert _wait_until(
            lambda: manager.status(first_station_id)["ticks"] >= 1
            and manager.status(second_station_id)["ticks"] >= 1
        )

        first_pid = manager.status(first_station_id)["pid"]
        second_pid = manager.status(second_station_id)["pid"]
        first_generation = manager.status(first_station_id)["generation"]
        with manager._lock:
            first_process = manager._states[first_station_id]["process"]
        first_process.terminate()
        first_process.wait(timeout=5.0)

        assert _wait_until(
            lambda: manager.status(first_station_id)["running"]
            and manager.status(first_station_id)["generation"] > first_generation
            and manager.status(first_station_id)["pid"] != first_pid
        )
        assert manager.status(first_station_id)["restart_count"] >= 1
        assert manager.status(second_station_id)["pid"] == second_pid
        assert manager.status(second_station_id)["running"] is True

        with manager._lock:
            config = manager._states[first_station_id]["config_path"].read_text(
                encoding="utf-8"
            )
            command = list(manager._states[first_station_id]["process"].args)
            state_root = manager._state_root
        assert "rpc_authkey" in config
        assert all("rpc_authkey" not in str(argument) for argument in command)
        assert all("credential://" not in str(argument) for argument in command)
        assert len(list(state_root.glob(f"station-{first_station_id}-g*.json"))) == 1
    finally:
        result = manager.stop_all()
        assert result["stopped"] == len(result["stations"])
