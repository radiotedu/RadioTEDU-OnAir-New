import json
import sqlite3
from pathlib import Path

from app.migration.legacy_import import (
    _normalize_track_type,
    _parse_bool,
    import_legacy_database,
)


def test_legacy_normalizers_preserve_numeric_false_and_announcement_type():
    assert _parse_bool(0, True) is False
    assert _parse_bool("0", True) is False
    assert _parse_bool(None, True) is True
    assert _normalize_track_type("announcement") == "announcement"
from app.repositories.station_output_repo import StationOutputRepository


def _create_legacy_db(db_path: Path, media_root: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    media_root.mkdir(parents=True, exist_ok=True)
    music_file = media_root / "music" / "morning-drive.mp3"
    music_file.parent.mkdir(parents=True, exist_ok=True)
    music_file.write_bytes(b"music")

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
    cur.execute(
        "INSERT INTO stations (id, name, slug, description, created_at) VALUES (1, 'Main Radio', 'main', 'Primary station', '2026-02-11 12:00:00')"
    )
    cur.execute(
        "INSERT INTO tracks (id, station_id, file_path, title, artist, album, genre, language, bpm, duration, track_type, added_at, modified_at, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            10,
            1,
            str(music_file),
            "Morning Drive",
            "DJ Example",
            "Wake Up",
            "Pop",
            "en",
            118.5,
            185.0,
            "music",
            "2026-02-12 09:00:00",
            "2026-02-12 09:00:00",
            1,
        ),
    )
    cur.execute(
        "INSERT INTO playlists (id, station_id, name, description, playlist_type, created_at) "
        "VALUES (20, 1, 'Drive Time', 'Morning rotation', 'manual', '2026-02-13 07:30:00')"
    )
    cur.execute(
        "INSERT INTO playlist_items (id, playlist_id, track_id, position, added_at) "
        "VALUES (21, 20, 10, 1, '2026-02-13 07:31:00')"
    )
    cur.execute(
        "INSERT INTO schedule (id, station_id, playlist_id, event_name, day_of_week, start_time, end_time, is_active, created_at) "
        "VALUES (30, 1, 20, 'Drive Time', '1,2,3,4,5', '09:00', '10:00', 1, '2026-02-13 08:00:00')"
    )
    cur.executemany(
        "INSERT INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?)",
        [
            ("ui_language", "tr-TR", "2026-02-14 09:00:00"),
            ("default_crossfade_seconds", "4.5", "2026-02-14 09:00:00"),
        ],
    )
    cur.executemany(
        "INSERT INTO station_settings (id, station_id, setting_key, setting_value, updated_at) VALUES (?, ?, ?, ?, ?)",
        [
            (40, 1, "output_mode", "icecast", "2026-02-14 09:00:00"),
            (41, 1, "speaker_monitor_enabled", "1", "2026-02-14 09:00:00"),
            (42, 1, "icecast_host", "127.0.0.1", "2026-02-14 09:00:00"),
            (43, 1, "icecast_port", "8000", "2026-02-14 09:00:00"),
            (44, 1, "icecast_mount", "/main", "2026-02-14 09:00:00"),
            (45, 1, "icecast_username", "source", "2026-02-14 09:00:00"),
            (46, 1, "icecast_password", "secret", "2026-02-14 09:00:00"),
            (47, 1, "output_gain_db", "2.5", "2026-02-14 09:00:00"),
        ],
    )
    cur.execute(
        "INSERT INTO metadata_rules (id, scope, station_id, name, target_field, match_type, pattern, replacement, is_case_sensitive, priority, is_active, created_at, updated_at) "
        "VALUES (50, 'station', 1, 'Trim suffix', 'title', 'regex', '\\\\s+live$', '', 0, 25, 1, '2026-02-15 11:00:00', '2026-02-15 11:00:00')"
    )
    cur.execute(
        "INSERT INTO ad_break_sets (id, station_id, name, description, intro_jingle_track_id, outro_jingle_track_id, is_active, created_at, updated_at) "
        "VALUES (60, 1, 'Drive Ads', 'Quarter-hour breaks', NULL, NULL, 1, '2026-02-16 12:00:00', '2026-02-16 12:00:00')"
    )
    cur.execute(
        "INSERT INTO ad_break_slots (id, break_set_id, slot_time, day_of_week, position, is_active, created_at, updated_at) "
        "VALUES (61, 60, '00:15', '*', 0, 1, '2026-02-16 12:01:00', '2026-02-16 12:01:00')"
    )
    cur.execute(
        "INSERT INTO ad_campaigns (id, station_id, name, start_date, end_date, play_times, day_interval, daily_repeat_limit, priority, is_active, notes, created_at, updated_at) "
        "VALUES (70, 1, 'Sponsor Flight', '2026-02-20', '2026-03-20', '00:15', 1, 0, 5, 1, 'Prime sponsor', '2026-02-16 12:05:00', '2026-02-16 12:05:00')"
    )
    cur.execute(
        "INSERT INTO ad_campaign_items (id, campaign_id, track_id, position, weight, is_active, created_at) "
        "VALUES (71, 70, 10, 0, 1, 1, '2026-02-16 12:06:00')"
    )
    cur.execute(
        "INSERT INTO ad_campaign_slots (id, campaign_id, slot_id, created_at) "
        "VALUES (72, 70, 61, '2026-02-16 12:07:00')"
    )
    conn.commit()
    conn.close()


def test_import_legacy_database_populates_cleanroom_core_tables(tmp_path):
    legacy_root = tmp_path / "legacy-product"
    legacy_db_path = legacy_root / "data" / "legacy.db"
    cleanroom_db_path = tmp_path / "cleanroom-data" / "cleanroom.db"

    _create_legacy_db(legacy_db_path, legacy_root / "media")

    summary = import_legacy_database(
        legacy_db_path=legacy_db_path,
        cleanroom_db_path=cleanroom_db_path,
    )

    assert summary["stations"] == 1
    assert summary["tracks"] == 1
    assert summary["playlists"] == 1
    assert summary["playlist_items"] == 1
    assert summary["schedule_items"] == 1
    assert summary["system_settings"] == 2
    assert summary["station_settings"] == 8
    assert summary["station_outputs"] == 1
    assert summary["metadata_rules"] == 1
    assert summary["ad_break_sets"] == 1
    assert summary["ad_campaigns"] == 1
    assert summary["media_copied"] == 0
    assert isinstance(summary["warnings"], list)

    conn = sqlite3.connect(str(cleanroom_db_path))
    conn.row_factory = sqlite3.Row

    assert conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM playlist_items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM schedule_items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM metadata_rules").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ad_break_sets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ad_campaigns").fetchone()[0] == 1

    track = conn.execute(
        "SELECT id, station_id, title, artist, album, genre, language, duration, bpm, track_type, is_active, file_path "
        "FROM tracks WHERE id=10"
    ).fetchone()
    assert dict(track) == {
        "id": 10,
        "station_id": 1,
        "title": "Morning Drive",
        "artist": "DJ Example",
        "album": "Wake Up",
        "genre": "Pop",
        "language": "en",
        "duration": 185.0,
        "bpm": 118.5,
        "track_type": "music",
        "is_active": 1,
        "file_path": str(legacy_root / "media" / "music" / "morning-drive.mp3"),
    }

    schedule = conn.execute(
        "SELECT id, station_id, track_id, status, play_at, window_end FROM schedule_items WHERE id=30"
    ).fetchone()
    assert int(schedule["station_id"]) == 1
    assert int(schedule["track_id"]) == 20
    assert str(schedule["status"]) == "pending"
    assert "09:00:00" in str(schedule["play_at"])
    assert "10:00:00" in str(schedule["window_end"])

    assert (
        conn.execute("SELECT value FROM system_settings WHERE key='ui_language'").fetchone()[0]
        == "tr-TR"
    )
    station_output = conn.execute(
        "SELECT station_id, local_output_enabled, output_device_id, icecast_enabled, icecast_host, icecast_port, icecast_mount, icecast_user, icecast_password, output_gain_db "
        "FROM station_outputs WHERE station_id=1"
    ).fetchone()
    output_payload = dict(station_output)
    assert str(output_payload.pop("icecast_password")).startswith(
        "credential://user/station/1/"
    )
    assert output_payload == {
        "station_id": 1,
        "local_output_enabled": 1,
        "output_device_id": "",
        "icecast_enabled": 1,
        "icecast_host": "127.0.0.1",
        "icecast_port": 8000,
        "icecast_mount": "/main",
        "icecast_user": "source",
        "output_gain_db": 2.5,
    }
    assert (
        StationOutputRepository(conn).get(1)["icecast_password"]
        == "secret"
    )
    assert conn.execute(
        "SELECT value FROM station_settings "
        "WHERE station_id=1 AND key='icecast_password'"
    ).fetchone()[0] == ""

    break_payload = json.loads(
        conn.execute("SELECT payload_json FROM ad_break_sets WHERE id=60").fetchone()[0]
    )
    assert break_payload["description"] == "Quarter-hour breaks"
    assert break_payload["slots"] == [
        {
            "slot_time": "00:15",
            "day_of_week": "*",
            "position": 0,
            "is_active": True,
        }
    ]

    campaign_payload = json.loads(
        conn.execute("SELECT payload_json FROM ad_campaigns WHERE id=70").fetchone()[0]
    )
    assert campaign_payload["slot_ids"] == [61]
    assert campaign_payload["track_ids"] == [10]
    assert campaign_payload["tracks"] == [
        {
            "track_id": 10,
            "id": 10,
            "title": "Morning Drive",
            "artist": "DJ Example",
            "duration": 185.0,
        }
    ]

    conn.close()
