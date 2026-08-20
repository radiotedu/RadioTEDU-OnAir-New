from fastapi.testclient import TestClient

from app.main import app
import app.api.runtime as runtime_api


class _FakeLoopManager:
    def __init__(self):
        self.running: dict[int, bool] = {}
        self.ticks: dict[int, int] = {}

    def start(self, station_id: int, fallback_uri: str, interval_sec: float):
        sid = int(station_id)
        self.running[sid] = True
        self.ticks.setdefault(sid, 0)
        return {
            "station_id": sid,
            "running": True,
            "interval_sec": interval_sec,
            "ticks": self.ticks[sid],
            "last_result": {"source": "none"},
            "last_error": "",
        }

    def stop(self, station_id: int):
        sid = int(station_id)
        self.running[sid] = False
        return {
            "station_id": sid,
            "running": False,
            "interval_sec": 1.0,
            "ticks": self.ticks.get(sid, 0),
            "last_result": {"source": "none"},
            "last_error": "",
        }

    def status(self, station_id: int):
        sid = int(station_id)
        return {
            "station_id": sid,
            "running": bool(self.running.get(sid, False)),
            "interval_sec": 1.0,
            "ticks": self.ticks.get(sid, 0),
            "last_result": {"source": "none"},
            "last_error": "",
        }


class _FakeRegistry:
    def status(self, station_id: int):
        return {
            "station_id": int(station_id),
            "running": False,
            "backend": "fake",
            "transition_mode": "none",
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }


def test_runtime_loop_start_status_stop_endpoints(monkeypatch):
    fake = _FakeLoopManager()
    fake_runtime = _FakeRegistry()
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake)
    monkeypatch.setattr(runtime_api, "runtime_registry", fake_runtime)
    client = TestClient(app)

    start = client.post(
        "/api/runtime/1/operator-start",
        json={"fallback_uri": "C:/music/fallback.mp3", "interval_sec": 0.5},
    )
    assert start.status_code == 200
    assert start.json()["running"] is True

    status = client.get("/api/runtime/1/loop/status")
    assert status.status_code == 200
    assert status.json()["running"] is True
    assert status.json()["runtime"]["station_id"] == 1

    stop = client.post("/api/runtime/1/loop/stop")
    assert stop.status_code == 200
    assert stop.json()["running"] is False


def test_runtime_loop_status_is_station_scoped(monkeypatch):
    fake = _FakeLoopManager()
    fake_runtime = _FakeRegistry()
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake)
    monkeypatch.setattr(runtime_api, "runtime_registry", fake_runtime)
    client = TestClient(app)

    start1 = client.post(
        "/api/runtime/1/operator-start",
        json={"fallback_uri": "C:/music/a.mp3", "interval_sec": 0.5},
    )
    start2 = client.post(
        "/api/runtime/2/operator-start",
        json={"fallback_uri": "C:/music/b.mp3", "interval_sec": 0.5},
    )

    assert start1.status_code == 200
    assert start2.status_code == 200
    assert client.get("/api/runtime/1/loop/status").json()["running"] is True
    assert client.get("/api/runtime/2/loop/status").json()["running"] is True

    stop1 = client.post("/api/runtime/1/loop/stop")
    assert stop1.status_code == 200
    assert client.get("/api/runtime/1/loop/status").json()["running"] is False
    assert client.get("/api/runtime/2/loop/status").json()["running"] is True


def test_unattended_loop_start_requires_station_restart_authorization(monkeypatch):
    fake = _FakeLoopManager()
    fake_runtime = _FakeRegistry()
    monkeypatch.setattr(runtime_api, "worker_loop_manager", fake)
    monkeypatch.setattr(runtime_api, "runtime_registry", fake_runtime)
    monkeypatch.setattr(
        runtime_api,
        "_station_broadcast_autostart_enabled",
        lambda _conn, _station_id: False,
    )
    client = TestClient(app)

    blocked = client.post(
        "/api/runtime/1/loop/start",
        json={"fallback_uri": "C:/music/a.mp3", "interval_sec": 0.5},
    )

    assert blocked.status_code == 409
    assert "operator_authorization_required" in blocked.json()["detail"]
    assert fake.running == {}
