import base64
import json
import logging
import os
import time

from app.engine.process_runtime_facade import ProcessIsolatedRuntimeFacade
from app.engine.process_audio_bridge import (
    ProcessAudioBridgeClient,
    ProcessAudioBridgeHost,
)
from app.engine.process_worker_child import (
    StationProcessLease,
    _atomic_write_json as child_atomic_write_json,
    _process_runtime_command,
    _transport_is_healthy,
    run_station_worker_process,
)
from app.engine.process_worker_manager import ProcessIsolatedStationWorkerManager


def test_child_atomic_heartbeat_retries_transient_windows_replace_race(
    tmp_path, monkeypatch
):
    target = tmp_path / "station-4.heartbeat.json"
    real_replace = os.replace
    attempts = {"count": 0}

    def sharing_race(source, destination):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError("sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr("app.engine.process_worker_child.os.replace", sharing_race)
    child_atomic_write_json(target, {"station_id": 4, "running": True})
    assert attempts["count"] == 3
    assert json.loads(target.read_text(encoding="utf-8"))["running"] is True


class FakeRuntimeRegistry:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.stop_all_called = False

    def start_station(self, station_id, input_uri, **kwargs):
        self.started.append((station_id, input_uri, kwargs))
        return {"running": True, "station_id": station_id}

    def stop_station(self, station_id):
        self.stopped.append(station_id)
        return {"running": False, "station_id": station_id}

    def status(self, station_id):
        return {"running": False, "station_id": station_id}

    def is_process_running(self, station_id):
        return False

    def recover_station(self, station_id, **kwargs):
        return {"recovered": True, "station_id": station_id, **kwargs}

    def refresh_live_audio_settings(self, station_id):
        return {"station_id": station_id}

    def refresh_output_settings(self, station_id):
        return {
            "station_id": station_id,
            "producer_preserved": True,
        }

    def stop_all(self):
        self.stop_all_called = True
        return {"stopped": 1}

    def snapshot(self):
        return []


class FakeSupervisor:
    def __init__(self, registry):
        self.registry = registry

    def evaluate_station(self, station_id):
        return {"action": "none", "running": False, "station_id": station_id}


def test_station_process_lease_rejects_a_second_owner(tmp_path):
    first = StationProcessLease(tmp_path / "station-4.lease")
    second = StationProcessLease(tmp_path / "station-4.lease")
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        second.close()
        first.close()


def test_manager_adopts_a_live_atomic_heartbeat(tmp_path, monkeypatch):
    manager = ProcessIsolatedStationWorkerManager(FakeRuntimeRegistry())
    manager._state_root = tmp_path
    monkeypatch.setattr(manager, "_pid_is_alive", lambda pid: int(pid) == 4242)
    manager._atomic_write_json(
        manager._heartbeat_path(7),
        {
            "generation": 3,
            "pid": 4242,
            "runtime_status": {"output_feed_active": True, "running": True},
            "station_id": 7,
            "ticks": 19,
            "updated_epoch": time.time(),
        },
    )

    state = manager._adopt_existing_state(
        7,
        fallback_uri="silence://continuity",
        interval_sec=1.0,
    )
    assert state is not None
    manager._states[7] = state
    status = manager.status(7)
    assert status["adopted"] is True
    assert status["pid"] == 4242
    assert status["runtime_status"]["output_feed_active"] is True


def test_manager_keeps_adoptable_heartbeat_for_full_stall_window(
    tmp_path,
    monkeypatch,
):
    manager = ProcessIsolatedStationWorkerManager(FakeRuntimeRegistry())
    manager._state_root = tmp_path
    monkeypatch.setattr(manager, "_pid_is_alive", lambda pid: int(pid) == 4242)
    manager._atomic_write_json(
        manager._heartbeat_path(7),
        {
            "generation": 3,
            "pid": 4242,
            "runtime_status": {"output_feed_active": True, "running": True},
            "station_id": 7,
            "ticks": 19,
            "updated_epoch": time.time() - 30.0,
        },
    )

    heartbeat = manager._read_live_heartbeat(7)

    assert heartbeat["pid"] == 4242
    assert 29.0 <= heartbeat["heartbeat_age_seconds"] <= 31.0


def test_stop_force_terminates_unresponsive_adopted_worker(
    tmp_path,
    monkeypatch,
):
    manager = ProcessIsolatedStationWorkerManager(FakeRuntimeRegistry())
    manager._state_root = tmp_path
    alive = {4242: True}
    monkeypatch.setattr(
        manager,
        "_pid_is_alive",
        lambda pid: bool(alive.get(int(pid), False)),
    )

    def terminate(pid):
        alive[int(pid)] = False
        return True

    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        "app.engine.process_worker_manager._STOP_TIMEOUT_SECONDS",
        0.01,
    )
    manager._states[7] = {
        "adopted": True,
        "adopted_pid": 4242,
        "audio_bridge_host": None,
        "audio_bridge_path": tmp_path / "station-7.audio-bridge.bin",
        "desired_running": True,
        "heartbeat_path": tmp_path / "station-7.heartbeat.json",
        "last_error": "",
        "station_id": 7,
        "stop_path": tmp_path / "station-7.stop",
    }

    stopped = manager.stop(7)

    assert stopped["running"] is False
    assert manager.status(7)["running"] is False


def test_child_runtime_command_is_station_and_generation_fenced(tmp_path):
    command_path = tmp_path / "station.command.json"
    ack_path = tmp_path / "station.ack.json"
    config = {
        "ack_path": str(ack_path),
        "command_path": str(command_path),
        "generation": 4,
        "station_id": 9,
    }
    command_path.write_text(
        json.dumps(
            {
                "args": [9, "silence://continuity"],
                "command_id": "command-1",
                "generation": 4,
                "kwargs": {"stream_title": "Continuity"},
                "method": "start_station",
                "station_id": 9,
            }
        ),
        encoding="utf-8",
    )
    registry = FakeRuntimeRegistry()

    command_id = _process_runtime_command(
        config,
        registry,
        FakeSupervisor(registry),
        "",
    )

    assert command_id == "command-1"
    assert registry.started == [
        (9, "silence://continuity", {"stream_title": "Continuity"})
    ]
    acknowledgement = json.loads(ack_path.read_text(encoding="utf-8"))
    assert acknowledgement["ok"] is True
    assert acknowledgement["result"]["running"] is True

    command_path.write_text(
        json.dumps(
            {
                "args": [9],
                "command_id": "command-2",
                "generation": 3,
                "kwargs": {},
                "method": "stop_station",
                "station_id": 9,
            }
        ),
        encoding="utf-8",
    )
    _process_runtime_command(config, registry, FakeSupervisor(registry), command_id)
    acknowledgement = json.loads(ack_path.read_text(encoding="utf-8"))
    assert acknowledgement["ok"] is False
    assert registry.stopped == []


def test_facade_reports_worker_owned_runtime_without_local_duplicate():
    local = FakeRuntimeRegistry()

    class WorkerManager:
        def status(self, station_id):
            return {
                "running": True,
                "runtime_status": {
                    "output_feed_active": True,
                    "running": True,
                    "station_id": station_id,
                },
                "station_id": station_id,
            }

        def snapshot(self):
            return [self.status(5)]

    facade = ProcessIsolatedRuntimeFacade(local, WorkerManager())

    status = facade.status(5)
    assert status["output_feed_active"] is True
    assert status["station_worker"]["running"] is True
    assert facade.is_process_running(5) is True


def test_facade_health_ignores_disabled_output_branches():
    local = FakeRuntimeRegistry()

    class WorkerManager:
        def status(self, station_id):
            return {
                "running": True,
                "runtime_status": {
                    "branch_health": {"icecast": True, "local": False},
                    "output_feed_active": True,
                    "required_outputs": {"icecast": True, "local": False},
                    "running": True,
                    "station_id": station_id,
                },
                "station_id": station_id,
            }

    facade = ProcessIsolatedRuntimeFacade(local, WorkerManager())

    assert facade.required_outputs_healthy(5) is True


def test_manager_accepts_same_generation_file_heartbeat_after_rpc_loss(
    tmp_path,
    monkeypatch,
):
    manager = ProcessIsolatedStationWorkerManager(FakeRuntimeRegistry())
    manager._state_root = tmp_path
    monkeypatch.setattr(manager, "_pid_is_alive", lambda pid: int(pid) == 4242)
    state = {
        "failure_count": 0,
        "generation": 7,
        "last_heartbeat_monotonic": 0.0,
        "station_id": 11,
        "ticks": 0,
    }
    manager._atomic_write_json(
        manager._heartbeat_path(11),
        {
            "failure_count": 0,
            "generation": 7,
            "pid": 4242,
            "runtime_status": {"output_feed_active": True, "running": True},
            "station_id": 11,
            "ticks": 91,
            "updated_epoch": time.time(),
        },
    )

    assert manager._refresh_from_file_heartbeat(state, expected_pid=4242) is True
    assert state["ticks"] == 91
    assert state["last_runtime_status"]["output_feed_active"] is True
    assert manager._refresh_from_file_heartbeat(state, expected_pid=9999) is False


def test_audio_bridge_moves_live_guest_and_return_pcm_without_rpc(tmp_path):
    class LiveMic:
        def snapshot(self, station_id):
            return {
                "active_user": {"id": 8, "username": "dj"},
                "level_db": -12.0,
                "live_input_enabled": True,
                "peak_db": -6.0,
                "receiving": True,
                "station_id": station_id,
                "transmitting": True,
            }

        def read_pcm(self, _station_id, requested):
            return b"\x01\x00" * (requested // 2)

    class Guests:
        def __init__(self):
            self.published = []

        def has_on_air(self, _station_id):
            return True

        def read_on_air_pcm(self, _station_id, requested):
            return b"\x02\x00" * (requested // 2)

        def publish_program_pcm(self, station_id, payload, **kwargs):
            self.published.append((station_id, payload, kwargs))

    guests = Guests()
    path = tmp_path / "station-1.audio-bridge.bin"
    host = ProcessAudioBridgeHost(
        path,
        1,
        live_mic_registry=LiveMic(),
        guest_audio_registry=guests,
        live_settings_provider=lambda _station_id: {"mic_gain": 0.75},
    )
    client = ProcessAudioBridgeClient(path)
    try:
        host._pump_once()
        assert client.snapshot(1)["transmitting"] is True
        assert client.read_pcm(1, 64) == b"\x01\x00" * 32
        assert client.has_on_air(1) is True
        assert client.read_on_air_pcm(1, 64) == b"\x02\x00" * 32

        program = b"\x03\x00\x04\x00" * 32
        client.publish_program_pcm(1, program)
        host._pump_once()
        assert guests.published[-1] == (1, program, {"voice_gain": 0.75})
    finally:
        client.close()
        host.close()


def test_facade_routes_soundboard_to_worker_owned_runtime():
    local = FakeRuntimeRegistry()

    class WorkerManager:
        def __init__(self):
            self.played = []
            self.stopped = []

        def status(self, station_id):
            return {"running": True, "station_id": station_id}

        def runtime_status(self, station_id):
            return {"program_running": True, "station_id": station_id}

        def soundboard_play(self, station_id, item):
            self.played.append((station_id, item))
            return True

        def soundboard_stop(self, station_id, item_id=None):
            self.stopped.append((station_id, item_id))
            return True

    manager = WorkerManager()
    facade = ProcessIsolatedRuntimeFacade(local, manager)
    player = facade.get_sound_effect_player(4)
    assert player is not None
    player.play({"id": 12, "name": "sting", "file_path": "sting.wav"})
    player.stop(item_id=12)
    assert manager.played[0][0] == 4
    assert manager.played[0][1]["id"] == 12
    assert manager.stopped == [(4, 12)]


def test_worker_process_owns_local_runtime_and_writes_final_heartbeat(
    tmp_path,
    monkeypatch,
):
    stop_path = tmp_path / "station-3.stop"
    stop_path.touch()
    heartbeat_path = tmp_path / "station-3.heartbeat.json"
    config = {
        "database_path": str(tmp_path / "cleanroom.db"),
        "data_root": str(tmp_path),
        "fallback_uri": "silence://continuity",
        "generation": 1,
        "heartbeat_path": str(heartbeat_path),
        "interval_sec": 0.1,
        "lock_path": str(tmp_path / "station-3.lease"),
        "log_path": str(tmp_path / "station-3.log"),
        "rpc_address": "unused",
        "rpc_authkey": base64.b64encode(b"a" * 32).decode("ascii"),
        "rpc_family": "AF_UNIX",
        "schema_version": 2,
        "station_id": 3,
        "stop_path": str(stop_path),
        "user_config_root": str(tmp_path),
        "worker_id": "worker-3",
    }
    registry = FakeRuntimeRegistry()
    monkeypatch.setattr(
        "app.engine.process_worker_child._load_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "app.engine.process_worker_child._configure_logging",
        lambda *_args: logging.getLogger("test-station-worker"),
    )
    monkeypatch.setattr("app.engine.process_worker_child.init_db", lambda: None)
    monkeypatch.setattr(
        "app.engine.process_worker_child.RemoteRuntimeRegistry",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("backend unavailable")),
    )
    monkeypatch.setattr(
        "app.engine.process_worker_child.StationRuntimeRegistry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "app.engine.process_worker_child.RuntimeSupervisor",
        FakeSupervisor,
    )

    assert run_station_worker_process() == 0
    assert registry.stop_all_called is True
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["running"] is False
    assert heartbeat["station_id"] == 3


def test_worker_liveness_requires_current_program_audio():
    healthy = {
        "program_running": True,
        "program_pcm_age_seconds": 0.2,
        "program_pcm_stalled": False,
        "required_outputs": {"icecast": True, "local": False},
        "icecast_mount_health": {
            "last_write_age_seconds": 0.1,
            "process_running": True,
            "writer_backpressured": False,
            "writer_failed": False,
            "writer_running": True,
        },
    }

    assert _transport_is_healthy(healthy) is True
    assert _transport_is_healthy({**healthy, "program_pcm_age_seconds": 8.0}) is False
    assert _transport_is_healthy(
        {
            **healthy,
            "icecast_mount_health": {
                **healthy["icecast_mount_health"],
                "writer_backpressured": True,
            },
        }
    ) is False
