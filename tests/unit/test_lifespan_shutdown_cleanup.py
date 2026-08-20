from fastapi.testclient import TestClient

from app.api import runtime as runtime_api
from app import main
from app.main import app


def test_startup_ai_is_not_loaded_when_every_station_disables_it(monkeypatch):
    called = {"ai": 0}

    class _UnexpectedAI:
        def preload_for_playout(self):
            called["ai"] += 1
            raise AssertionError("disabled AI must not be loaded")

    monkeypatch.setattr(main, "_any_station_ai_enabled", lambda _conn: False)
    monkeypatch.setattr(
        "app.services.ai_host_fast.get_ai_host_fast", lambda: _UnexpectedAI()
    )

    assert main._preload_startup_ai(object(), skip_startup_ai=False) is False
    assert called["ai"] == 0


def test_startup_ai_warms_only_after_an_operator_enables_it(monkeypatch):
    called = {"ai": 0}

    class _EnabledAI:
        def preload_for_playout(self):
            called["ai"] += 1
            return {
                "llm_loaded": True,
                "tts_provider": "test",
                "load_time_seconds": 0,
            }

    monkeypatch.setattr(main, "_any_station_ai_enabled", lambda _conn: True)
    monkeypatch.setattr(
        "app.services.ai_host_fast.get_ai_host_fast", lambda: _EnabledAI()
    )

    assert main._preload_startup_ai(object(), skip_startup_ai=False) is True
    assert called["ai"] == 1


def test_startup_prefetch_does_not_create_ai_service_when_disabled(monkeypatch):
    writes = []

    class _Stations:
        def __init__(self, _conn):
            pass

        def list_all(self):
            return [{"id": 1}, {"id": 2}]

    class _Settings:
        def __init__(self, _conn):
            pass

        def get_station(self, _station_id):
            return {"ai_host_enabled": "false"}

        def upsert_station(self, station_id, values):
            writes.append((station_id, dict(values)))

    def _unexpected_prefetch():
        raise AssertionError("disabled AI must not create the prefetch service")

    monkeypatch.setattr(main, "StationRepository", _Stations)
    monkeypatch.setattr(main, "SettingsRepository", _Settings)
    monkeypatch.setattr(
        "app.services.ai_prefetch.get_ai_prefetch", _unexpected_prefetch
    )

    assert main._prime_startup_ai_prefetch(object(), skip_startup_ai=False) == 0
    assert [station_id for station_id, _values in writes] == [1, 2]
    assert all(
        values["startup_ai_readiness_state"] == "disabled"
        for _station_id, values in writes
    )


def test_lifespan_shutdown_calls_runtime_cleanup(monkeypatch):
    called = {"loops": 0, "runtimes": 0}

    def _fake_loop_start(*, station_id, fallback_uri="", interval_sec=1.0):
        return {
            "station_id": int(station_id),
            "running": True,
            "interval_sec": float(interval_sec),
            "fallback_uri": str(fallback_uri),
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        }

    def _fake_loop_stop_all():
        called["loops"] += 1
        return {"stations": [], "stopped": 0}

    def _fake_runtime_stop_all():
        called["runtimes"] += 1
        return {"stations": [], "stopped": 0}

    monkeypatch.setattr(runtime_api.worker_loop_manager, "start", _fake_loop_start)
    monkeypatch.setattr(runtime_api.worker_loop_manager, "stop_all", _fake_loop_stop_all)
    monkeypatch.setattr(runtime_api.runtime_registry, "stop_all", _fake_runtime_stop_all)

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200

    assert called["loops"] == 1
    assert called["runtimes"] == 1


def test_lifespan_runs_dependency_bootstrap_once(monkeypatch):
    called = {"bootstrap": 0}

    def _fake_bootstrap_dependencies():
        called["bootstrap"] += 1
        return {}

    def _fake_loop_start(*, station_id, fallback_uri="", interval_sec=1.0):
        return {
            "station_id": int(station_id),
            "running": True,
            "interval_sec": float(interval_sec),
            "fallback_uri": str(fallback_uri),
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        }

    monkeypatch.setattr("app.main.bootstrap_dependencies", _fake_bootstrap_dependencies)
    monkeypatch.setattr(runtime_api.worker_loop_manager, "start", _fake_loop_start)

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200

    assert called["bootstrap"] == 1
