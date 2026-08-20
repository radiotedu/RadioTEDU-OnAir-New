import sqlite3

import pytest

from app.db import get_connection, init_db


def test_only_one_active_queue_item_can_own_a_dedupe_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO queue_items "
            "(station_id, track_id, position, status, dedupe_key) "
            "VALUES (1, 10, 1, 'pending', 'jingle:10:1')"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO queue_items "
                "(station_id, track_id, position, status, dedupe_key) "
                "VALUES (1, 10, 2, 'pending', 'jingle:10:1')"
            )

        conn.rollback()
        conn.execute(
            "UPDATE queue_items SET status='done', finished_at=CURRENT_TIMESTAMP "
            "WHERE station_id=1 AND dedupe_key='jingle:10:1'"
        )
        conn.execute(
            "INSERT INTO queue_items "
            "(station_id, track_id, position, status, dedupe_key) "
            "VALUES (1, 10, 3, 'pending', 'jingle:10:1')"
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_quarantines_preexisting_active_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE queue_items ("
            "id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, "
            "track_id INTEGER NOT NULL, position INTEGER NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', enqueued_at TEXT, "
            "started_at TEXT, finished_at TEXT, dedupe_key TEXT)"
        )
        conn.executemany(
            "INSERT INTO queue_items "
            "(id, station_id, track_id, position, status, dedupe_key) "
            "VALUES (?, 1, 10, ?, ?, 'startup_sound:10')",
            [(1, 1, "pending"), (2, 2, "playing"), (3, 3, "pending")],
        )
        conn.commit()

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(path))
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, status FROM queue_items ORDER BY id"
        ).fetchall()
        assert [(int(row["id"]), str(row["status"])) for row in rows] == [
            (1, "failed"),
            (2, "playing"),
            (3, "failed"),
        ]
    finally:
        conn.close()
