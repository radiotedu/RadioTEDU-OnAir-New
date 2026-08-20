import sqlite3

from app.engine.broadcast_queue_autofill import _purge_inactive_pending_queue_items


def test_purge_inactive_pending_preserves_playing_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, is_active INTEGER);
        CREATE TABLE queue_items (
            id INTEGER PRIMARY KEY,
            station_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        INSERT INTO tracks VALUES (1, 0), (2, 0), (3, 1);
        INSERT INTO queue_items VALUES
            (10, 4, 1, 'playing'),
            (11, 4, 2, 'pending'),
            (12, 4, 3, 'pending'),
            (13, 5, 2, 'pending');
        """
    )

    removed = _purge_inactive_pending_queue_items(conn, 4)

    assert removed == 1
    assert conn.execute(
        "SELECT id FROM queue_items ORDER BY id"
    ).fetchall() == [(10,), (12,), (13,)]
