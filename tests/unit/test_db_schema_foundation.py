import sqlite3

from app.db import get_connection, init_db


def test_foundation_tables_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert "station_worker_lease" in names
    assert "playout_state" in names
    assert "schedule_items" in names
    assert "ad_break_items" in names
    assert "command_outbox" in names


def test_connection_supports_a_bounded_best_effort_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    conn = get_connection(timeout_seconds=0.25)
    try:
        busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        conn.close()

    assert busy_timeout == 250


def test_init_db_migrates_legacy_queue_items_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE queue_items (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, position INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    init_db()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(queue_items)")
    names = {row[1] for row in cur.fetchall()}
    assert "status" in names
    assert "enqueued_at" in names
    assert "started_at" in names
    assert "finished_at" in names
    assert "dedupe_key" in names


def test_init_db_is_safe_to_reenter_while_another_writer_holds_lock(tmp_path, monkeypatch):
    db_path = tmp_path / "locked.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    init_db()

    lock_conn = sqlite3.connect(str(db_path), timeout=0.1)
    try:
        lock_conn.execute("BEGIN IMMEDIATE")
        init_db()
    finally:
        lock_conn.rollback()
        lock_conn.close()
