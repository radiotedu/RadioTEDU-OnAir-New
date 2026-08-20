import sqlite3
from pathlib import Path

from tools.commission_wall_media import (
    _deactivate_missing_media,
    _ensure_jingle_intervals,
    _validation_snapshot,
)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE station_outputs (
            station_id INTEGER PRIMARY KEY,
            icecast_enabled INTEGER,
            icecast_host TEXT,
            icecast_port INTEGER,
            icecast_mount TEXT
        );
        CREATE TABLE station_settings (
            station_id INTEGER,
            key TEXT,
            value TEXT,
            updated_at TEXT,
            UNIQUE(station_id, key)
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            station_id INTEGER,
            track_type TEXT,
            is_active INTEGER,
            file_path TEXT
        );
        CREATE TABLE queue_items (
            id INTEGER PRIMARY KEY,
            station_id INTEGER,
            track_id INTEGER,
            status TEXT
        );
        CREATE TABLE program_queue_items (
            id INTEGER PRIMARY KEY,
            station_id INTEGER,
            track_id INTEGER
        );
        CREATE TABLE schedule_items (
            id INTEGER PRIMARY KEY,
            station_id INTEGER,
            track_id INTEGER,
            status TEXT
        );
        """
    )
    conn.row_factory = sqlite3.Row


def test_missing_media_is_deactivated_and_pending_references_removed(
    tmp_path: Path,
):
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"audio")
    conn.execute("INSERT INTO stations VALUES (1, 'Station')")
    conn.executemany(
        "INSERT INTO tracks VALUES (?, 1, 'music', 1, ?)",
        [(1, str(existing)), (2, str(tmp_path / "missing.mp3"))],
    )
    conn.execute("INSERT INTO queue_items VALUES (1, 1, 2, 'pending')")
    conn.execute("INSERT INTO program_queue_items VALUES (1, 1, 2)")
    conn.execute("INSERT INTO schedule_items VALUES (1, 1, 2, 'pending')")

    summary = _deactivate_missing_media(conn)

    assert summary["deactivated"] == 1
    assert conn.execute("SELECT is_active FROM tracks WHERE id=2").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM program_queue_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM schedule_items").fetchone()[0] == 0


def test_missing_sweeper_interval_defaults_to_two():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO stations VALUES (1, 'Station')")

    summary = _ensure_jingle_intervals(conn)

    assert summary == {"inserted": 1, "normalized": 0}
    assert (
        conn.execute(
            "SELECT value FROM station_settings "
            "WHERE station_id=1 AND key='sweeper_interval'"
        ).fetchone()[0]
        == "2"
    )


def test_validation_rejects_duplicate_enabled_mounts(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    media = tmp_path / "song.mp3"
    media.write_bytes(b"audio")
    for station_id in (1, 2):
        conn.execute("INSERT INTO stations VALUES (?, ?)", (station_id, f"S{station_id}"))
        conn.execute(
            "INSERT INTO station_outputs VALUES (?, 1, 'icecast', 8000, '/same')",
            (station_id,),
        )
        conn.execute(
            "INSERT INTO tracks VALUES (?, ?, 'music', 1, ?)",
            (station_id * 10, station_id, str(media)),
        )
        conn.execute(
            "INSERT INTO tracks VALUES (?, ?, 'jingle', 1, ?)",
            (station_id * 10 + 1, station_id, str(media)),
        )

    snapshot = _validation_snapshot(conn)

    assert snapshot["ok"] is False
    assert any("share mount" in error for error in snapshot["errors"])
