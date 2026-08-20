from fastapi.testclient import TestClient

from app.api import runtime as runtime_api
from app.auth.dependencies import is_public_api_path
from app.db import get_connection, init_db
from app.main import app
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository
from app.repositories.station_repo import StationRepository
from tests.conftest import _should_auto_auth


def test_public_station_summary_path_is_not_auto_authed_by_the_test_client():
    assert _should_auto_auth("/api/public/stations") is False
    assert _should_auto_auth("/api/public/stations/") is False


def _seed_public_station_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        stations = StationRepository(conn)
        live_station_id = 1
        degraded_station_id = stations.create("Degraded FM")
        offline_station_id = stations.create("Offline FM")

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tracks (station_id, title, artist, duration, track_type, is_active, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (live_station_id, "Sunrise", "The Dawn", 180.0, "music", 1, "C:/music/sunrise.mp3"),
        )
        track_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO queue_items (station_id, track_id, position, status, started_at) "
            "VALUES (?, ?, ?, 'playing', CURRENT_TIMESTAMP)",
            (live_station_id, track_id, 1),
        )
        cur.execute(
            "INSERT INTO tracks (station_id, title, artist, duration, track_type, is_active, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (offline_station_id, "Preserved Song", "Queue Artist", 120.0, "music", 1, "C:/music/preserved.mp3"),
        )
        offline_track_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO queue_items (station_id, track_id, position, status, started_at) "
            "VALUES (?, ?, ?, 'playing', CURRENT_TIMESTAMP)",
            (offline_station_id, offline_track_id, 1),
        )

        show_repo = ShowRepository(conn)
        show_id = show_repo.create(live_station_id, "Morning Drive")
        session_repo = ShowSessionRepository(conn)
        session_id = session_repo.create(show_id, live_station_id, 1)
        session_repo.update_status(session_id, "live")

        conn.commit()
        return live_station_id, degraded_station_id, offline_station_id
    finally:
        conn.close()


def test_public_station_summary_is_unauthenticated_and_public_safe(tmp_path, monkeypatch):
    live_station_id, degraded_station_id, offline_station_id = _seed_public_station_state(
        tmp_path, monkeypatch
    )

    runtime_states = {
        live_station_id: {
            "station_id": live_station_id,
            "running": True,
            "backend": "ffmpeg",
            "transition_mode": "start",
            "transition_active": False,
            "branch_health": {"icecast": True, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        },
        degraded_station_id: {
            "station_id": degraded_station_id,
            "running": True,
            "backend": "ffmpeg",
            "transition_mode": "start",
            "transition_active": False,
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        },
        offline_station_id: {
            "station_id": offline_station_id,
            "running": False,
            "backend": "none",
            "transition_mode": "none",
            "transition_active": False,
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        },
    }

    worker_states = {
        live_station_id: {
            "station_id": live_station_id,
            "running": True,
            "interval_sec": 1.0,
            "fallback_uri": "",
            "ticks": 3,
            "last_result": {"source": "playing"},
            "last_error": "",
        },
        degraded_station_id: {
            "station_id": degraded_station_id,
            "running": True,
            "interval_sec": 1.0,
            "fallback_uri": "",
            "ticks": 2,
            "last_result": {"source": "playing"},
            "last_error": "runtime health degraded",
        },
        offline_station_id: {
            "station_id": offline_station_id,
            "running": False,
            "interval_sec": 1.0,
            "fallback_uri": "",
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        },
    }

    monkeypatch.setattr(runtime_api.runtime_registry, "status", lambda station_id: runtime_states[int(station_id)])
    monkeypatch.setattr(runtime_api.runtime_registry, "snapshot", lambda: list(runtime_states.values()))
    monkeypatch.setattr(runtime_api.worker_loop_manager, "status", lambda station_id: worker_states[int(station_id)])
    monkeypatch.setattr(runtime_api.worker_loop_manager, "snapshot", lambda: list(worker_states.values()))

    client = TestClient(app)
    res = client.get("/api/public/stations")

    assert res.status_code == 200
    payload = res.json()
    assert "stations" in payload
    stations = payload["stations"]
    assert len(stations) == 3

    live = next(item for item in stations if item["id"] == live_station_id)
    degraded = next(item for item in stations if item["id"] == degraded_station_id)
    offline = next(item for item in stations if item["id"] == offline_station_id)

    expected_keys = {
        "id",
        "name",
        "status",
        "status_reason",
        "now_playing",
        "preserved_item",
        "active_show_name",
    }
    for item in stations:
        assert set(item.keys()) == expected_keys

    assert live["status"] == "live"
    assert degraded["status"] == "degraded"
    assert offline["status"] == "offline"
    assert live["now_playing"] is not None
    assert live["preserved_item"] is None
    assert str(live["now_playing"]["started_at"]).endswith("Z")
    assert degraded["now_playing"] is None
    assert offline["now_playing"] is None
    assert offline["preserved_item"]["title"] == "Preserved Song"
    assert live["active_show_name"] == "Morning Drive"
    assert degraded["active_show_name"] is None
    assert offline["active_show_name"] is None

    for item in stations:
        assert "access_token" not in item
        assert "refresh_token" not in item
        assert "password_hash" not in item
        assert "user_sessions" not in item
        assert "icecast_password" not in item
        assert "session" not in item


def test_public_api_path_policy_keeps_station_summary_public_with_or_without_trailing_slash():
    assert is_public_api_path("/api/public/stations") is True
    assert is_public_api_path("/api/public/stations/") is True
    assert is_public_api_path("/api/public/other") is False
