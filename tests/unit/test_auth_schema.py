from app.db import get_connection, init_db


def test_init_db_creates_auth_tables_and_default_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    init_db()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert "users" in names
    assert "user_sessions" in names
    assert "api_keys" in names

    cur.execute(
        "SELECT username, role, is_active, password_hash FROM users WHERE username='admin'"
    )
    row = cur.fetchone()
    assert row is not None
    assert str(row["username"]) == "admin"
    assert str(row["role"]) == "admin"
    assert int(row["is_active"]) == 1
    assert str(row["password_hash"]) != "changeme"
    assert str(row["password_hash"]).strip()
    conn.close()
