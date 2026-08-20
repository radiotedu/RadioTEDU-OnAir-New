from fastapi.testclient import TestClient

import app.api.health as health_api
import app.api.runtime as runtime_api
from app.engine.runtime_registry import StationRuntimeRegistry
from app.engine.worker_loop import StationWorkerLoopManager
from app.main import app


class _FakeRuntime:
    def __init__(self):
        self.started = False

    def start(self, cfg, **_kwargs):
        self.started = True

    def stop(self):
        self.started = False

    def is_running(self):
        return self.started

    def branch_health(self):
        return {"icecast": True, "local": False}

    def status(self):
        return {
            "running": self.started,
            "backend": "fake",
            "transition_mode": "none",
            "branch_health": self.branch_health(),
        }


def test_health_reports_requested_station_runtime_without_overwriting_other_station(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    registry = StationRuntimeRegistry(runtime_factory=lambda: _FakeRuntime())
    loops = StationWorkerLoopManager(runtime_registry=registry)
    monkeypatch.setattr(runtime_api, "runtime_registry", registry)
    monkeypatch.setattr(runtime_api, "worker_loop_manager", loops)
    monkeypatch.setattr(health_api, "runtime_registry", registry)
    monkeypatch.setattr(health_api, "worker_loop_manager", loops)

    client = TestClient(app)
    stations = client.get("/api/stations")
    assert stations.status_code == 200
    created = client.post("/api/stations", json={"name": "Station Two"})
    assert created.status_code == 200
    station_two_id = int(created.json()["id"])

    start1 = client.post(
        "/api/runtime/1/operator-start-track",
        json={"input_uri": "C:/music/a.mp3"},
    )
    start2 = client.post(
        f"/api/runtime/{station_two_id}/operator-start-track",
        json={"input_uri": "C:/music/b.mp3"},
    )
    assert start1.status_code == 200
    assert start2.status_code == 200

    health1 = client.get("/api/health", params={"station_id": 1})
    health2 = client.get("/api/health", params={"station_id": station_two_id})

    assert health1.status_code == 200
    assert health2.status_code == 200
    payload1 = health1.json()
    payload2 = health2.json()
    assert payload1["station_id"] == 1
    assert payload2["station_id"] == station_two_id
    assert payload2["station_name"] == "Station Two"
    assert payload1["runtime"]["station_id"] == 1
    assert payload2["runtime"]["station_id"] == station_two_id
    assert payload1["engine_running"] is True
    assert payload2["engine_running"] is True
    assert len(payload2["runtime_registry"]) == 2
