import pytest

from app.db import get_connection, init_db
from app.repositories.station_repo import StationRepository


def test_delete_station_rejects_the_last_station(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        repo = StationRepository(conn)
        station_id = int(repo.list_all()[0]["id"])

        with pytest.raises(ValueError, match="last station"):
            repo.delete(station_id)
    finally:
        conn.close()


def test_delete_active_station_promotes_replacement_and_cleans_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        repo = StationRepository(conn)
        first_id = int(repo.list_all()[0]["id"])
        second_id = int(repo.create("Second Station"))
        repo.set_active(first_id)

        conn.execute(
            "INSERT INTO station_outputs (station_id, local_output_enabled, output_device_id) VALUES (?, ?, ?)",
            (first_id, 1, "device-a"),
        )
        conn.execute(
            "INSERT INTO station_settings (station_id, key, value) VALUES (?, ?, ?)",
            (first_id, "duck_db", "-12"),
        )
        conn.execute(
            "INSERT INTO tracks (station_id, title, artist, track_type, duration, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            (first_id, "Track A", "Artist", "music", 123.0, 1),
        )
        conn.execute(
            "INSERT INTO playlists (station_id, name) VALUES (?, ?)",
            (first_id, "Playlist A"),
        )
        conn.execute(
            "INSERT INTO shows (station_id, name) VALUES (?, ?)",
            (first_id, "Show A"),
        )
        show_id = int(conn.execute("SELECT id FROM shows WHERE station_id=?", (first_id,)).fetchone()[0])
        user_id = int(conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0])
        conn.execute(
            "INSERT INTO show_sessions (show_id, station_id, user_id, status) VALUES (?, ?, ?, ?)",
            (show_id, first_id, user_id, "preparing"),
        )
        conn.execute(
            "INSERT INTO soundboard_items (station_id, name, file_path) VALUES (?, ?, ?)",
            (first_id, "Hit", "C:/tmp/hit.mp3"),
        )
        conn.commit()

        replacement_id = repo.delete(first_id)

        assert replacement_id == second_id
        assert int(repo.get_active()["id"]) == second_id
        assert conn.execute("SELECT COUNT(*) FROM stations WHERE id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM station_outputs WHERE station_id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM station_settings WHERE station_id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tracks WHERE station_id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM playlists WHERE station_id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM shows WHERE station_id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM show_sessions WHERE station_id=?", (first_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM soundboard_items WHERE station_id=?", (first_id,)).fetchone()[0] == 0
    finally:
        conn.close()
