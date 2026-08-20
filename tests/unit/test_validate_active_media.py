import sqlite3

from tools import validate_active_media


def test_validate_active_media_reports_playable_and_missing_files(tmp_path, monkeypatch):
    database = tmp_path / "cleanroom.db"
    playable = tmp_path / "song.mp3"
    playable.write_bytes(b"test")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE tracks ("
            "id INTEGER, station_id INTEGER, track_type TEXT, title TEXT, "
            "file_path TEXT, is_active INTEGER)"
        )
        conn.executemany(
            "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 2, "music", "Song", str(playable), 1),
                (2, 2, "music", "Missing", str(tmp_path / "missing.mp3"), 1),
                (3, 2, "music", "Inactive", str(tmp_path / "inactive.mp3"), 0),
            ],
        )
    monkeypatch.setattr(
        validate_active_media,
        "_get_audio_metadata",
        lambda *_args, **_kwargs: {"duration": 12.5},
    )

    report = validate_active_media.validate(database, workers=2)

    assert report["ok"] is False
    assert report["active_rows"] == 2
    assert report["unique_files"] == 2
    assert report["playable_files"] == 1
    assert report["failed_files"] == 1
    assert report["playable_by_station_and_type"] == {"2:music": 1}
    assert report["failures"][0]["error"] == "missing"
