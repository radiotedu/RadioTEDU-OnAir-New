import threading

from app.db import get_connection, init_db
from app.main import _autostart_station_worker_loops
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_repo import StationRepository


def _stations():
    init_db()
    conn = get_connection()
    stations = StationRepository(conn)
    settings = SettingsRepository(conn)
    first = stations.create("Cold boot healthy")
    second = stations.create("Cold boot pending")
    for sid in (first, second):
        settings.upsert_station(sid, {"broadcast_autostart_enabled": "true"})
    return conn, first, second


def test_retry_roster_excludes_healthy_stations(monkeypatch):
    conn, first, second = _stations()
    started = []
    monkeypatch.setattr("app.main.resolve_station_fallback_uri", lambda **kw: "")
    monkeypatch.setattr(
        "app.api.runtime.worker_loop_manager.start",
        lambda **kw: started.append(kw["station_id"]),
    )
    try:
        pending = _autostart_station_worker_loops(conn, station_ids={second})
        assert pending == set()
        assert started == [second]
        assert first not in started
    finally:
        conn.close()


def test_failed_boot_returns_pending_station_after_starting_siblings(monkeypatch):
    conn, first, second = _stations()
    started = []
    monkeypatch.setattr("app.main.resolve_station_fallback_uri", lambda **kw: "")
    monkeypatch.setattr("app.main.time.sleep", lambda seconds: None)

    def start(**kw):
        started.append(kw["station_id"])
        if kw["station_id"] == second:
            raise OSError("media volume not ready")

    monkeypatch.setattr("app.api.runtime.worker_loop_manager.start", start)
    try:
        pending = _autostart_station_worker_loops(conn, station_ids={first, second})
        assert pending == {second}
        assert started.count(first) == 1
        assert started.count(second) == 2
    finally:
        conn.close()


def test_operator_disabling_autostart_during_failure_cancels_retries(monkeypatch):
    conn, first, second = _stations()
    started = []
    monkeypatch.setattr("app.main.resolve_station_fallback_uri", lambda **kw: "")
    monkeypatch.setattr("app.main.time.sleep", lambda seconds: None)

    def start(**kw):
        started.append(kw["station_id"])
        SettingsRepository(conn).upsert_station(
            second, {"broadcast_autostart_enabled": "false"}
        )
        raise OSError("media volume not ready")

    monkeypatch.setattr("app.api.runtime.worker_loop_manager.start", start)
    try:
        assert _autostart_station_worker_loops(conn, station_ids={second}) == set()
        assert started == [second]
    finally:
        conn.close()


def test_shutdown_between_channels_prevents_another_start(monkeypatch):
    conn, first, second = _stations()
    stop = threading.Event()
    started = []
    monkeypatch.setattr("app.main.resolve_station_fallback_uri", lambda **kw: "")

    def start(**kw):
        started.append(kw["station_id"])
        stop.set()

    monkeypatch.setattr("app.api.runtime.worker_loop_manager.start", start)
    try:
        assert _autostart_station_worker_loops(
            conn, station_ids={first, second}, stop_event=stop
        ) == set()
        assert started == [first]
    finally:
        conn.close()
