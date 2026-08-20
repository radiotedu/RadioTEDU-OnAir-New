from fastapi.testclient import TestClient

from app.main import app
import app.api.runtime as runtime_api


class _FakeRegistry:
    def __init__(self, raise_on_start: bool = False):
        self.running: dict[int, bool] = {}
        self.raise_on_start = raise_on_start

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
    ):
        if self.raise_on_start:
            raise ValueError("no output targets enabled")
        self.running[int(station_id)] = True
        return {
            "station_id": station_id,
            "running": True,
            "backend": "fake",
            "transition_mode": "start",
            "branch_health": {"icecast": True, "local": True},
            "required_outputs": {"icecast": True, "local": True},
        }

    def stop_station(self, station_id: int):
        self.running[int(station_id)] = False
        return {
            "station_id": station_id,
            "running": False,
            "backend": "fake",
            "transition_mode": "stop",
            "branch_health": {"icecast": True, "local": True},
            "required_outputs": {"icecast": True, "local": True},
        }

    def status(self, station_id: int):
        return {
            "station_id": station_id,
            "running": bool(self.running.get(int(station_id), False)),
            "backend": "fake",
            "transition_mode": "none",
            "branch_health": {"icecast": True, "local": True},
            "required_outputs": {"icecast": True, "local": True},
        }


class _FakeSupervisor:
    def evaluate_station(self, station_id: int):
        return {"station_id": station_id, "action": "none"}


class _FakeLoopManager:
    def status(self, station_id: int):
        return {
            "station_id": station_id,
            "running": False,
            "interval_sec": None,
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        }


class _RaisingWorker:
    def __init__(self, *args, **kwargs):
        pass

    def process_once(self):
        raise FileNotFoundError("gst-launch-1.0")


def test_runtime_start_stop_status_endpoints(monkeypatch):
    fake = _FakeRegistry()
    fake_supervisor = _FakeSupervisor()
    fake_loops = _FakeLoopManager()
    monkeypatch.setattr(runtime_api, "runtime_registry", fake)
    monkeypatch.setattr(runtime_api, "runtime_supervisor", fake_supervisor)
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake_loops)
    client = TestClient(app)

    start = client.post(
        "/api/runtime/1/operator-start-track",
        json={"input_uri": "C:/music/fallback.mp3"},
    )
    assert start.status_code == 200
    assert start.json()["running"] is True

    status = client.get("/api/runtime/1/status")
    assert status.status_code == 200
    assert status.json()["running"] is True
    assert status.json()["worker_loop"]["station_id"] == 1

    stop = client.post("/api/runtime/1/stop")
    assert stop.status_code == 200
    assert stop.json()["running"] is False

    supervise = client.post("/api/runtime/1/operator-supervise")
    assert supervise.status_code == 200
    assert supervise.json()["action"] == "none"


def test_runtime_tick_returns_503_when_gstreamer_missing(monkeypatch):
    fake = _FakeRegistry()
    fake_supervisor = _FakeSupervisor()
    fake_loops = _FakeLoopManager()
    monkeypatch.setattr(runtime_api, "runtime_registry", fake)
    monkeypatch.setattr(runtime_api, "runtime_supervisor", fake_supervisor)
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake_loops)
    monkeypatch.setattr(runtime_api, "StationWorker", _RaisingWorker)
    monkeypatch.setattr(
        runtime_api,
        "_require_unattended_start_authorization",
        lambda _station_id: None,
    )
    client = TestClient(app)
    tick = client.post("/api/runtime/1/tick", json={"fallback_uri": "C:/music/fallback.mp3"})
    assert tick.status_code == 503


def test_runtime_start_returns_400_for_invalid_output_config(monkeypatch):
    fake = _FakeRegistry(raise_on_start=True)
    fake_supervisor = _FakeSupervisor()
    fake_loops = _FakeLoopManager()
    monkeypatch.setattr(runtime_api, "runtime_registry", fake)
    monkeypatch.setattr(runtime_api, "runtime_supervisor", fake_supervisor)
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake_loops)
    client = TestClient(app)
    start = client.post(
        "/api/runtime/1/operator-start-track",
        json={"input_uri": "C:/music/fallback.mp3"},
    )
    assert start.status_code == 400


def test_runtime_status_is_station_scoped(monkeypatch):
    fake = _FakeRegistry()
    fake_supervisor = _FakeSupervisor()
    fake_loops = _FakeLoopManager()
    monkeypatch.setattr(runtime_api, "runtime_registry", fake)
    monkeypatch.setattr(runtime_api, "runtime_supervisor", fake_supervisor)
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake_loops)
    client = TestClient(app)

    start1 = client.post(
        "/api/runtime/1/operator-start-track",
        json={"input_uri": "C:/music/a.mp3"},
    )
    start2 = client.post(
        "/api/runtime/2/operator-start-track",
        json={"input_uri": "C:/music/b.mp3"},
    )

    assert start1.status_code == 200
    assert start2.status_code == 200
    assert client.get("/api/runtime/1/status").json()["running"] is True
    assert client.get("/api/runtime/2/status").json()["running"] is True

    stop1 = client.post("/api/runtime/1/stop")
    assert stop1.status_code == 200
    assert client.get("/api/runtime/1/status").json()["running"] is False
    assert client.get("/api/runtime/2/status").json()["running"] is True


def test_unattended_runtime_start_paths_require_restart_authorization(monkeypatch):
    fake = _FakeRegistry()
    fake_supervisor = _FakeSupervisor()
    fake_loops = _FakeLoopManager()
    monkeypatch.setattr(runtime_api, "runtime_registry", fake)
    monkeypatch.setattr(runtime_api, "runtime_supervisor", fake_supervisor)
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake_loops)
    monkeypatch.setattr(
        runtime_api,
        "_station_broadcast_autostart_enabled",
        lambda _conn, _station_id: False,
    )
    client = TestClient(app)

    start = client.post(
        "/api/runtime/1/start",
        json={"input_uri": "C:/music/fallback.mp3"},
    )
    supervise = client.post("/api/runtime/1/supervise")
    tick = client.post(
        "/api/runtime/1/tick",
        json={"fallback_uri": "C:/music/fallback.mp3"},
    )

    assert start.status_code == 409
    assert supervise.status_code == 409
    assert tick.status_code == 409
    assert fake.running == {}
