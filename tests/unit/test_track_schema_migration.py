import sqlite3

from app.db import get_connection, init_db


def test_init_db_migrates_tracks_with_file_path(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy_tracks.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT DEFAULT '', artist TEXT DEFAULT '', musicbrainz_recordingid TEXT DEFAULT '')"
    )
    conn.commit()
    conn.close()

    init_db()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tracks)")
    cols = {row[1] for row in cur.fetchall()}
    assert "file_path" in cols
