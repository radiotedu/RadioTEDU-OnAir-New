from app.db import get_connection, init_db


def test_core_tables_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert "stations" in names
    assert "station_outputs" in names
    assert "tracks" in names
    assert "queue_items" in names


def test_init_db_creates_a_real_default_station(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id, name FROM stations ORDER BY id ASC").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert str(rows[0]["name"]) == "Main Radio"
