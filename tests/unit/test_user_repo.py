from app.db import get_connection, init_db
from app.repositories.user_repo import SessionRepository, UserRepository


def test_session_repository_creates_and_revokes_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()

    conn = get_connection()
    users = UserRepository(conn)
    sessions = SessionRepository(conn)

    user_id = users.create_user("dj-a", "DJ A", "hash", "dj")
    session_id = sessions.create_session(
        user_id=user_id,
        refresh_token="refresh-1",
        device_info="pytest",
        ip_address="127.0.0.1",
        expires_at="2099-01-01 00:00:00",
    )

    row = sessions.get_session_by_token("refresh-1")
    assert row is not None
    assert int(row["id"]) == session_id
    assert int(row["user_id"]) == user_id
    stored = conn.execute(
        "SELECT refresh_token FROM user_sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    assert stored is not None
    assert str(stored["refresh_token"]) != "refresh-1"
    assert len(str(stored["refresh_token"])) == 64

    assert sessions.revoke_session(session_id) is True
    revoked = sessions.get_session_by_token("refresh-1")
    assert revoked is not None
    assert int(revoked["revoked"]) == 1
    conn.close()
