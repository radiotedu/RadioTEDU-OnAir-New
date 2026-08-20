"""Tests for ShowRepository CRUD operations."""
from pathlib import Path

import pytest

from app.db import init_db, get_connection


def _setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    init_db()
    return get_connection()


def test_shows_table_exists(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shows'")
        assert cur.fetchone() is not None
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='show_assignments'")
        assert cur.fetchone() is not None
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='show_sessions'")
        assert cur.fetchone() is not None
    finally:
        conn.close()


from app.repositories.show_repo import ShowRepository
from app.repositories.station_repo import StationRepository
from app.repositories.user_repo import UserRepository


def _make_station(conn, name="Test FM"):
    return StationRepository(conn).create(name)


def _make_user(conn, username="dj1", role="dj"):
    repo = UserRepository(conn)
    repo.create_user(username, username.title(), "x", role)
    return repo.get_user_by_username(username)


def test_create_and_get_show(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="Morning Show", description="Wake up!")
        show = repo.get(show_id)
        assert show is not None
        assert show["name"] == "Morning Show"
        assert show["station_id"] == sid
        assert show["is_active"] == 1
    finally:
        conn.close()


def test_list_by_station(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        repo.create(station_id=sid, name="Show A")
        repo.create(station_id=sid, name="Show B")
        shows = repo.list_by_station(sid)
        assert len(shows) == 2
        names = {s["name"] for s in shows}
        assert names == {"Show A", "Show B"}
    finally:
        conn.close()


def test_update_show(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="Old Name")
        updated = repo.update(show_id, name="New Name", color="#ff0000")
        assert updated["name"] == "New Name"
        assert updated["color"] == "#ff0000"
    finally:
        conn.close()


def test_delete_show(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="Doomed")
        assert repo.delete(show_id) is True
        assert repo.get(show_id) is None
    finally:
        conn.close()


def test_assign_and_list_assigned(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="DJ Show")
        user = _make_user(conn, "dj1", "dj")
        repo.assign(show_id, user["id"], role="dj")
        assignments = repo.list_assignments(show_id)
        assert len(assignments) == 1
        assert assignments[0]["user_id"] == user["id"]
        assert assignments[0]["role"] == "dj"
    finally:
        conn.close()


def test_list_shows_for_user(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        s1 = repo.create(station_id=sid, name="Assigned Show")
        s2 = repo.create(station_id=sid, name="Other Show")
        user = _make_user(conn, "dj1", "dj")
        repo.assign(s1, user["id"], role="dj")
        shows = repo.list_for_user(user["id"], station_id=sid)
        assert len(shows) == 1
        assert shows[0]["id"] == s1
    finally:
        conn.close()


def test_unassign(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="Show")
        user = _make_user(conn, "dj1", "dj")
        repo.assign(show_id, user["id"], role="dj")
        assert repo.unassign(show_id, user["id"]) is True
        assert len(repo.list_assignments(show_id)) == 0
    finally:
        conn.close()


def test_duplicate_assign_upserts_role(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="Show")
        user = _make_user(conn, "dj1", "dj")
        repo.assign(show_id, user["id"], role="dj")
        repo.assign(show_id, user["id"], role="producer")
        assignments = repo.list_assignments(show_id)
        assert len(assignments) == 1
        assert assignments[0]["role"] == "producer"
    finally:
        conn.close()


def test_is_assigned(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        sid = _make_station(conn)
        repo = ShowRepository(conn)
        show_id = repo.create(station_id=sid, name="Show")
        user = _make_user(conn, "dj1", "dj")
        assert repo.is_assigned(show_id, user["id"]) is False
        repo.assign(show_id, user["id"], role="dj")
        assert repo.is_assigned(show_id, user["id"]) is True
    finally:
        conn.close()


def test_get_nonexistent_show_returns_none(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        repo = ShowRepository(conn)
        assert repo.get(9999) is None
    finally:
        conn.close()


def test_delete_nonexistent_show_returns_false(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, monkeypatch)
    try:
        repo = ShowRepository(conn)
        assert repo.delete(9999) is False
    finally:
        conn.close()
