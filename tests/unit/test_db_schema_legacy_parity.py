import sqlite3

from app.db import get_connection, init_db


def test_legacy_parity_tables_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert "playlists" in names
    assert "playlist_items" in names
    assert "system_settings" in names
    assert "station_settings" in names
    assert "operation_logs" in names
    assert "program_queue_items" in names
    assert "ad_break_sets" in names
    assert "ad_campaigns" in names
    assert "studios" in names
    assert "studio_sessions" in names
    assert "studio_chat_messages" in names


def test_playlists_table_has_parity_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(playlists)")
    cols = {str(row[1]) for row in cur.fetchall()}
    assert "description" in cols
    assert "playlist_type" in cols


def test_ad_break_sets_table_has_payload_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(ad_break_sets)")
    cols = {str(row[1]) for row in cur.fetchall()}
    assert "payload_json" in cols


def test_tracks_table_has_autoplay_rotation_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tracks)")
    cols = {str(row[1]) for row in cur.fetchall()}
    assert "exclude_from_autoplay" in cols
    assert "play_count" in cols
    assert "last_played_at" in cols


def test_studios_table_has_coordination_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(studios)")
    cols = {str(row[1]) for row in cur.fetchall()}
    assert "station_id" in cols
    assert "name" in cols
    assert "description" in cols
    assert "sort_order" in cols
    assert "is_active" in cols
    assert "is_on_air" in cols
    assert "current_user_id" in cols


def test_studio_sessions_table_has_coordination_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(studio_sessions)")
    cols = {str(row[1]) for row in cur.fetchall()}
    assert "studio_id" in cols
    assert "user_id" in cols
    assert "session_role" in cols
    assert "status" in cols
    assert "joined_at" in cols
    assert "left_at" in cols
    assert "last_seen_at" in cols


def test_studio_chat_messages_table_has_coordination_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(studio_chat_messages)")
    cols = {str(row[1]) for row in cur.fetchall()}
    assert "studio_id" in cols
    assert "user_id" in cols
    assert "message" in cols
    assert "created_at" in cols


def test_init_db_backfills_default_studios_for_existing_stations(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO stations (id, name) VALUES (1, 'Alpha FM')")
        conn.execute("INSERT INTO stations (id, name) VALUES (2, 'Beta FM')")
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    finally:
        conn.close()

    init_db()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT station_id, name, is_on_air FROM studios ORDER BY station_id ASC")
    rows = cur.fetchall()
    conn.close()

    assert len(rows) == 2
    assert [int(row["station_id"]) for row in rows] == [1, 2]
    assert all(row["name"] == "Studio A" for row in rows)
    assert all(int(row["is_on_air"]) == 1 for row in rows)
