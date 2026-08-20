import pytest

from app.db import get_connection, init_db
from app.repositories.station_repo import StationRepository
from app.repositories.studio_repo import StudioRepository
from app.repositories.user_repo import UserRepository


def test_studio_repo_bootstraps_default_studio_for_new_station(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()
    conn = get_connection()
    try:
        station_id = StationRepository(conn).create("Night FM")
        rows = StudioRepository(conn).list_by_station(station_id)
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["name"] == "Studio A"
    assert int(rows[0]["is_on_air"]) == 1


def test_studio_repo_persists_sessions_and_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()
    conn = get_connection()
    try:
        user_id = UserRepository(conn).create_user("talk-fm-host", "Talk FM Host", "hash", "dj")
        station_id = StationRepository(conn).create("Talk FM")
        repo = StudioRepository(conn)
        studio = repo.list_by_station(station_id)[0]
        session_id = repo.upsert_session(
            studio_id=int(studio["id"]),
            user_id=user_id,
            session_role="dj",
            status="active",
        )
        message_id = repo.create_chat_message(int(studio["id"]), user_id, "stand by")
        sessions = repo.list_sessions(int(studio["id"]))
        messages = repo.list_chat_messages(int(studio["id"]), limit=10)
    finally:
        conn.close()

    assert session_id > 0
    assert message_id > 0
    assert len(sessions) == 1
    assert int(sessions[0]["user_id"]) == user_id
    assert len(messages) == 1
    assert messages[0]["message"] == "stand by"


def test_station_create_rolls_back_if_default_studio_creation_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()

    from app.repositories import studio_repo as studio_repo_module

    original_create = studio_repo_module.StudioRepository.create

    def flaky_create(self, *args, **kwargs):
        result = original_create(self, *args, **kwargs)
        raise RuntimeError("default studio creation failed")

    monkeypatch.setattr(studio_repo_module.StudioRepository, "create", flaky_create)

    conn = get_connection()
    with pytest.raises(RuntimeError):
        StationRepository(conn).create("Fail FM")
    conn.close()

    verify_conn = get_connection()
    try:
        stations = StationRepository(verify_conn).list_all()
        studios = StudioRepository(verify_conn).list_by_station(1)
    finally:
        verify_conn.close()

    assert len(stations) == 1
    assert str(stations[0]["name"]) == "Main Radio"
    assert len(studios) == 1


def test_studio_repo_update_whitelists_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()
    conn = get_connection()
    try:
        station_id = StationRepository(conn).create("Update FM")
        repo = StudioRepository(conn)
        studio = repo.list_by_station(station_id)[0]

        assert repo.update(int(studio["id"]), name="Studio B", sort_order=2) is True
        assert repo.update(int(studio["id"]), not_a_column="boom") is False

        updated = repo.get(int(studio["id"]))
    finally:
        conn.close()

    assert updated["name"] == "Studio B"
    assert int(updated["sort_order"]) == 2


def test_studio_repo_delete_removes_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()
    conn = get_connection()
    try:
        station_id = StationRepository(conn).create("Delete FM")
        repo = StudioRepository(conn)
        studio = repo.create(station_id=station_id, name="Studio B", is_on_air=False)

        assert repo.delete(int(studio)) is True
        assert repo.get(int(studio)) is None
        assert all(int(row["id"]) != int(studio) for row in repo.list_by_station(station_id))
    finally:
        conn.close()
