import time

from app.engine.process_worker_manager import ProcessIsolatedStationWorkerManager
from app.engine.runtime_registry import StationRuntimeRegistry


def test_station_worker_process_is_adopted_without_duplicate_source_owner(
    tmp_path,
    monkeypatch,
):
    data_root = tmp_path / "data"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(data_root / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_OPEN_PANEL", "0")

    first = ProcessIsolatedStationWorkerManager(StationRuntimeRegistry())
    second = None
    original_pid = None
    try:
        started = first.start(
            station_id=1,
            fallback_uri="silence://continuity",
            interval_sec=0.2,
        )
        assert started["running"] is True
        assert started["adopted"] is False
        original_pid = int(started["pid"])

        heartbeat_path = first._heartbeat_path(1)
        deadline = time.monotonic() + 5.0
        while not heartbeat_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert heartbeat_path.is_file()

        # Model loss of the first backend's in-memory ownership without asking
        # the station process to stop. The protected heartbeat and lease remain.
        first._states[1]["desired_running"] = False
        monitor = first._states[1].get("monitor_thread")
        if monitor is not None:
            monitor.join(timeout=2.0)
        # Model OS cleanup of the crashed backend's mapped bridge while leaving
        # the autonomous child process and its mapping alive for adoption.
        first_bridge = first._states[1].get("audio_bridge_host")
        if first_bridge is not None:
            first_bridge.close()
            first._states[1]["audio_bridge_host"] = None

        second = ProcessIsolatedStationWorkerManager(StationRuntimeRegistry())
        adopted = second.start(
            station_id=1,
            fallback_uri="silence://continuity",
            interval_sec=0.2,
        )
        assert adopted["running"] is True
        assert adopted["adopted"] is True
        assert int(adopted["pid"]) == original_pid
        runtime = second._issue_runtime_command(1, "status", 1)
        assert isinstance(runtime, dict)
        assert int(runtime.get("station_id") or 1) == 1
    finally:
        if second is not None:
            second.stop(1)
        else:
            first.stop(1)

    assert original_pid is not None
    assert first._pid_is_alive(original_pid) is False
