# tests/unit/test_show_session_repo.py
"""Tests for ShowSessionRepository CRUD operations."""
import pytest

from app.db import init_db, get_connection


def _setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    init_db()
    return get_connection()


def _create_station_and_show(conn):
    """Create a station and show for testing."""
    from app.repositories.station_repo import StationRepository
    from app.repositories.show_repo import ShowRepository

    station_id = StationRepository(conn).create("Test FM")
    show_id = ShowRepository(conn).create(station_id, "Morning Show")
    return station_id, show_id


from app.repositories.show_session_repo import ShowSessionRepository


def test_create_session(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        assert session_id > 0
        session = repo.get(session_id)
        assert session is not None
        assert session["show_id"] == show_id
        assert session["station_id"] == station_id
        assert session["user_id"] == 1
        assert session["status"] == "preparing"
        assert session["started_at"] is None
        assert session["ended_at"] is None
    finally:
        conn.close()


def test_get_active_for_station(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        active = repo.get_active_for_station(station_id)
        assert active is not None
        assert active["id"] == session_id
    finally:
        conn.close()


def test_get_active_for_station_returns_none_when_ended(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        repo.end_session(session_id)
        active = repo.get_active_for_station(station_id)
        assert active is None
    finally:
        conn.close()


def test_update_status(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        updated = repo.update_status(session_id, "going_live")
        assert updated["status"] == "going_live"
    finally:
        conn.close()


def test_update_status_to_live_sets_started_at(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        repo.update_status(session_id, "going_live")
        updated = repo.update_status(session_id, "live")
        assert updated["started_at"] is not None
    finally:
        conn.close()


def test_update_status_to_live_second_time_keeps_original_started_at(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        repo.update_status(session_id, "live")
        first = repo.get(session_id)
        # Go to break and back to live
        repo.update_status(session_id, "break_outro")
        repo.update_status(session_id, "on_break")
        repo.update_status(session_id, "break_intro")
        repo.update_status(session_id, "live")
        second = repo.get(session_id)
        assert first["started_at"] == second["started_at"]
    finally:
        conn.close()


def test_end_session(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        session_id = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        ended = repo.end_session(session_id)
        assert ended["status"] == "ended"
        assert ended["ended_at"] is not None
    finally:
        conn.close()


def test_concurrent_session_prevented(tmp_path, monkeypatch):
    """The unique index prevents two non-ended sessions on the same station."""
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        repo.create(show_id=show_id, station_id=station_id, user_id=1)
        with pytest.raises(Exception):
            repo.create(show_id=show_id, station_id=station_id, user_id=2)
    finally:
        conn.close()


def test_list_for_show(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        s1 = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        repo.end_session(s1)
        s2 = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        sessions = repo.list_for_show(show_id)
        assert len(sessions) == 2
        assert sessions[0]["id"] == s2  # newest first
    finally:
        conn.close()


def test_end_stale_sessions(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        s1 = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        repo.update_status(s1, "live")
        count = repo.end_stale_sessions()
        assert count >= 1
        session = repo.get(s1)
        assert session["status"] == "ended"
        assert session["ended_at"] is not None
    finally:
        conn.close()


def test_end_stale_sessions_preserves_preparing(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        station_id, show_id = _create_station_and_show(conn)
        repo = ShowSessionRepository(conn)
        s1 = repo.create(show_id=show_id, station_id=station_id, user_id=1)
        # status is 'preparing' — should NOT be ended
        count = repo.end_stale_sessions()
        assert count == 0
        session = repo.get(s1)
        assert session["status"] == "preparing"
    finally:
        conn.close()


def test_show_ws_events_importable():
    from app.ws.events import (
        EVENT_SHOW_PREPARING,
        EVENT_SHOW_GOING_LIVE,
        EVENT_SHOW_INTRO_PLAYING,
        EVENT_SHOW_LIVE,
        EVENT_SHOW_BREAK_START,
        EVENT_SHOW_BREAK_END,
        EVENT_SHOW_OUTRO_PLAYING,
        EVENT_SHOW_ENDED,
        EVENT_SHOW_QUEUE_LOW,
        EVENT_AD_BREAK_UPCOMING,
        EVENT_AD_BREAK_MISSED,
    )
    assert EVENT_SHOW_LIVE == "show.live"
