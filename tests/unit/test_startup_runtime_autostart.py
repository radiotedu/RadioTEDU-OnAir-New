import threading
import time

from fastapi.testclient import TestClient

from app.main import app
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_repo import StationRepository


def _capture_worker_loop_start(monkeypatch):
    started = []

    def _fake_start(*, station_id, fallback_uri="", interval_sec=1.0, readiness_timeout_seconds=45.0):
        started.append(
            {
                "station_id": int(station_id),
                "fallback_uri": str(fallback_uri),
                "interval_sec": float(interval_sec),
            }
        )
        return {
            "station_id": int(station_id),
            "running": True,
            "interval_sec": float(interval_sec),
            "fallback_uri": str(fallback_uri),
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        }

    monkeypatch.setattr("app.main.bootstrap_dependencies", lambda: {})
    monkeypatch.setattr("app.api.runtime.worker_loop_manager.start", _fake_start)
    return started



def _wait_for_started(started, count):
    deadline = time.monotonic() + 3
    while len(started) < count and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert len(started) == count, "Background boot task did not start the authorized roster"

def test_normal_startup_does_not_apply_a_legacy_codec_migration(monkeypatch):
    calls = []
    _capture_worker_loop_start(monkeypatch)
    monkeypatch.delenv('CLEANROOM_ENABLE_LEGACY_CODEC_MIGRATION', raising=False)
    monkeypatch.setattr('app.services.codec_migration.migrate_ogg_outputs_to_he_aac', lambda *a, **k: calls.append(True) or {})
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
    assert calls == []


def test_lifespan_keeps_default_station_stopped_until_operator_enables_autostart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    started = _capture_worker_loop_start(monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200

    assert started == []


def test_lifespan_autostarts_worker_loops_for_all_stations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    started = _capture_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    repo = StationRepository(conn)
    station_id = repo.create("Station 2")
    station_3_id = repo.create("Station 3")
    repo.set_active(station_id)
    settings = SettingsRepository(conn)
    for sid in (1, station_id, station_3_id):
        settings.upsert_station(sid, {"broadcast_autostart_enabled": "true"})

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200
        _wait_for_started(started, 3)

    assert started == [
        {
            "station_id": 1,
            "fallback_uri": "silence://continuous",
                "interval_sec": 0.1,
        },
        {
            "station_id": 2,
            "fallback_uri": "silence://continuous",
                "interval_sec": 0.1,
        },
        {
            "station_id": 3,
            "fallback_uri": "silence://continuous",
                "interval_sec": 0.1,
        }
    ]


def test_lifespan_autostarts_worker_loop_with_station_specific_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    started = _capture_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "continuity_fallback_uri": "C:/music/station-one-fallback.mp3",
            "broadcast_autostart_enabled": "true",
        },
    )

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200
        _wait_for_started(started, 1)

    assert started == [
        {
            "station_id": 1,
            "fallback_uri": "C:/music/station-one-fallback.mp3",
                "interval_sec": 0.1,
        }
    ]
