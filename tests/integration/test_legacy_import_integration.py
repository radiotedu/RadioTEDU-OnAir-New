import sqlite3
from pathlib import Path

from app.migration.legacy_import import import_legacy_database


def _create_legacy_db_for_media_copy(db_path: Path, legacy_root: Path) -> None:
    media_root = legacy_root / "media"
    global_music = media_root / "music" / "anthem.mp3"
    station_ad = media_root / "stations" / "test-2" / "ads" / "promo.mp3"
    global_music.parent.mkdir(parents=True, exist_ok=True)
    station_ad.parent.mkdir(parents=True, exist_ok=True)
    global_music.write_bytes(b"anthem")
    station_ad.write_bytes(b"promo")

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE stations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            description TEXT DEFAULT '',
            logo_url TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            station_id INTEGER DEFAULT 1,
            file_path TEXT NOT NULL UNIQUE,
            title TEXT DEFAULT '',
            artist TEXT DEFAULT '',
            album TEXT DEFAULT '',
            genre TEXT DEFAULT '',
            language TEXT DEFAULT '',
            year INTEGER DEFAULT 0,
            bpm REAL DEFAULT 0,
            duration REAL DEFAULT 0,
            bitrate INTEGER DEFAULT 0,
            sample_rate INTEGER DEFAULT 0,
            channels INTEGER DEFAULT 2,
            intro_point REAL DEFAULT 0,
            outro_point REAL DEFAULT 0,
            cue_in REAL DEFAULT 0,
            cue_out REAL DEFAULT 0,
            gain REAL DEFAULT 0,
            track_type TEXT DEFAULT 'music',
            file_size INTEGER DEFAULT 0,
            file_hash TEXT DEFAULT '',
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            modified_at TEXT DEFAULT CURRENT_TIMESTAMP,
            play_count INTEGER DEFAULT 0,
            last_played TEXT DEFAULT NULL,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE playlists (
            id INTEGER PRIMARY KEY,
            station_id INTEGER DEFAULT 1,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            playlist_type TEXT DEFAULT 'manual',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            modified_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE playlist_items (
            id INTEGER PRIMARY KEY,
            playlist_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE schedule (
            id INTEGER PRIMARY KEY,
            station_id INTEGER DEFAULT 1,
            playlist_id INTEGER NOT NULL,
            event_name TEXT DEFAULT '',
            day_of_week TEXT DEFAULT '*',
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE system_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE station_settings (
            id INTEGER PRIMARY KEY,
            station_id INTEGER NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE metadata_rules (
            id INTEGER PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'station',
            station_id INTEGER DEFAULT NULL,
            name TEXT NOT NULL DEFAULT '',
            target_field TEXT NOT NULL DEFAULT 'title',
            match_type TEXT NOT NULL DEFAULT 'contains',
            pattern TEXT NOT NULL DEFAULT '',
            replacement TEXT NOT NULL DEFAULT '',
            is_case_sensitive INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 100,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ad_break_sets (
            id INTEGER PRIMARY KEY,
            station_id INTEGER DEFAULT 1,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            intro_jingle_track_id INTEGER DEFAULT NULL,
            outro_jingle_track_id INTEGER DEFAULT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ad_break_slots (
            id INTEGER PRIMARY KEY,
            break_set_id INTEGER NOT NULL,
            slot_time TEXT NOT NULL,
            day_of_week TEXT DEFAULT '*',
            position INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ad_campaigns (
            id INTEGER PRIMARY KEY,
            station_id INTEGER DEFAULT 1,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            play_times TEXT DEFAULT '',
            day_interval INTEGER DEFAULT 1,
            daily_repeat_limit INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ad_campaign_items (
            id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            weight INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ad_campaign_slots (
            id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            slot_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    cur.executemany(
        "INSERT INTO stations (id, name, slug, description, created_at) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Main Radio", "main", "Main station", "2026-02-10 10:00:00"),
            (2, "Station Two", "test-2", "Second station", "2026-02-10 11:00:00"),
        ],
    )
    cur.executemany(
        "INSERT INTO tracks (id, station_id, file_path, title, artist, track_type, duration, bpm, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                10,
                1,
                r"C:\archived\radio-automation\media\music\anthem.mp3",
                "Anthem",
                "Main Artist",
                "music",
                201.0,
                120.0,
                1,
            ),
            (
                11,
                2,
                r"D:\old\radio-automation\media\stations\test-2\ads\promo.mp3",
                "Promo",
                "",
                "ad",
                30.0,
                0.0,
                1,
            ),
        ],
    )
    cur.executemany(
        "INSERT INTO station_settings (id, station_id, setting_key, setting_value, updated_at) VALUES (?, ?, ?, ?, ?)",
        [
            (1, 1, "output_mode", "icecast", "2026-02-20 09:00:00"),
            (2, 1, "speaker_monitor_enabled", "1", "2026-02-20 09:00:00"),
            (3, 1, "icecast_mount", "/main", "2026-02-20 09:00:00"),
            (4, 2, "output_mode", "icecast", "2026-02-20 09:00:00"),
            (5, 2, "speaker_monitor_enabled", "0", "2026-02-20 09:00:00"),
            (6, 2, "icecast_mount", "/station-two", "2026-02-20 09:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_import_legacy_database_copies_media_and_rewrites_paths(tmp_path):
    legacy_root = tmp_path / "legacy-product"
    legacy_db_path = legacy_root / "data" / "legacy.db"
    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    cleanroom_db_path = tmp_path / "portable-cleanroom" / "cleanroom.db"

    _create_legacy_db_for_media_copy(legacy_db_path, legacy_root)

    summary = import_legacy_database(
        legacy_db_path=legacy_db_path,
        cleanroom_db_path=cleanroom_db_path,
        copy_media=True,
    )

    target_media_root = cleanroom_db_path.parent / "media"
    copied_music = target_media_root / "music" / "anthem.mp3"
    copied_station_ad = target_media_root / "stations" / "test-2" / "ads" / "promo.mp3"

    assert summary["stations"] == 2
    assert summary["tracks"] == 2
    assert summary["station_outputs"] == 2
    assert summary["media_copied"] == 2
    assert copied_music.exists()
    assert copied_station_ad.exists()
    assert copied_music.read_bytes() == b"anthem"
    assert copied_station_ad.read_bytes() == b"promo"

    conn = sqlite3.connect(str(cleanroom_db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, station_id, track_type, file_path FROM tracks ORDER BY id ASC"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "id": 10,
            "station_id": 1,
            "track_type": "music",
            "file_path": str(copied_music.resolve()),
        },
        {
            "id": 11,
            "station_id": 2,
            "track_type": "ad",
            "file_path": str(copied_station_ad.resolve()),
        },
    ]

    outputs = conn.execute(
        "SELECT station_id, local_output_enabled, icecast_enabled, icecast_mount FROM station_outputs ORDER BY station_id ASC"
    ).fetchall()
    assert [dict(row) for row in outputs] == [
        {
            "station_id": 1,
            "local_output_enabled": 1,
            "icecast_enabled": 1,
            "icecast_mount": "/main",
        },
        {
            "station_id": 2,
            "local_output_enabled": 0,
            "icecast_enabled": 1,
            "icecast_mount": "/station-two",
        },
    ]

    conn.close()
