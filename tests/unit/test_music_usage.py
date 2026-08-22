import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import db as database
from app.services.music_usage import HASH_PAYLOAD_COLUMNS, MusicUsageService


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, station_id INTEGER, title TEXT, artist TEXT,
            duration REAL, track_type TEXT, file_path TEXT
        );
        CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE station_outputs (
            station_id INTEGER PRIMARY KEY, icecast_mount TEXT, icecast_enabled INTEGER
        );
        CREATE TABLE track_broadcast_metadata (
            track_id INTEGER PRIMARY KEY, version TEXT DEFAULT '', composer TEXT DEFAULT '',
            lyricist TEXT DEFAULT '', phonogram_producer TEXT DEFAULT '', label TEXT DEFAULT '',
            isrc TEXT DEFAULT '', source_reference TEXT DEFAULT '', rights_reference TEXT DEFAULT '',
            source_type TEXT DEFAULT '', notes TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE music_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER, queue_item_id INTEGER,
            track_id INTEGER, broadcast_at TEXT, work_title TEXT, version TEXT, performer TEXT,
            composer TEXT, lyricist TEXT, phonogram_producer TEXT, label TEXT, isrc TEXT,
            scheduled_duration_seconds REAL, played_duration_seconds REAL, publication_count INTEGER,
            source_path TEXT, source_reference TEXT, rights_reference TEXT, program_name TEXT,
            presenter TEXT, delivered_variants_json TEXT NOT NULL DEFAULT '[]',
            log_id TEXT UNIQUE, metadata_snapshot_json TEXT, previous_hash TEXT,
            entry_hash TEXT UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE music_usage_month_closures (
            period_key TEXT PRIMARY KEY, period_start TEXT, period_end TEXT, record_count INTEGER,
            first_entry_hash TEXT, last_entry_hash TEXT, export_path TEXT, checksum TEXT,
            closed_by TEXT, closed_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER trg_music_usage_log_no_update BEFORE UPDATE ON music_usage_log
        BEGIN SELECT RAISE(ABORT, 'music usage log is append-only'); END;
        CREATE TRIGGER trg_music_usage_log_no_delete BEFORE DELETE ON music_usage_log
        BEGIN SELECT RAISE(ABORT, 'music usage log is append-only'); END;
        INSERT INTO tracks VALUES (1, 2, 'Test Song', 'Test Artist', 222.0, 'music', 'C:/media/test.mp3');
        INSERT INTO stations VALUES (2, 'RadioTEDU Lo-Fi');
        INSERT INTO station_outputs VALUES (2, '/lofi', 1);
        INSERT INTO track_broadcast_metadata (track_id, version, composer, lyricist, phonogram_producer, label, isrc, source_reference, rights_reference)
        VALUES (1, 'Radio Edit', 'Composer', 'Lyricist', 'Producer', 'Label', 'TRAAA2600001', 'promo-email-42', 'license/2026-001');
        """
    )
    return conn


def test_completed_play_is_hash_chained_and_idempotent():
    conn = _db()
    service = MusicUsageService(conn)
    first = service.record_completed_play(
        station_id=2,
        track_id=1,
        queue_item_id=55,
        started_at="2026-08-09 14:28:22",
        finished_at="2026-08-09 14:32:00",
        program_name="Sabah Programı",
        presenter="Operator",
    )
    duplicate = service.record_completed_play(
        station_id=2, track_id=1, queue_item_id=55, finished_at="2026-08-09 14:32:00"
    )
    assert first["log_id"] == "queue:55"
    assert duplicate["id"] == first["id"]
    assert first["version"] == "Radio Edit"
    assert first["isrc"] == "TRAAA2600001"
    assert first["played_duration_seconds"] == pytest.approx(218.0)
    assert conn.execute("SELECT COUNT(*) FROM music_usage_log").fetchone()[0] == 1
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE music_usage_log SET work_title='tampered' WHERE id=1")


def test_csv_export_and_month_close_are_deterministic(tmp_path: Path):
    conn = _db()
    service = MusicUsageService(conn)
    service.record_completed_play(
        station_id=2, track_id=1, queue_item_id=1, finished_at="2026-08-09 14:32:00"
    )
    destination = tmp_path / "2026-08-09.csv"
    result = service.export_csv(
        destination=destination, date_from="2026-08-09", date_to="2026-08-10"
    )
    text = destination.read_text(encoding="utf-8")
    assert result["record_count"] == 1
    assert "work_title" in text.splitlines()[0]
    assert "Test Song" in text
    closed = service.close_month(year=2026, month=8, export_path=tmp_path / "2026-08.csv")
    assert closed["period_key"] == "2026-08"
    assert closed["record_count"] == 1
    assert Path(closed["export_path"]).is_file()
    assert service.close_month(year=2026, month=8)["period_key"] == "2026-08"


def test_official_current_report_has_mount_counts_and_valid_hash_chain(tmp_path: Path):
    conn = _db()
    service = MusicUsageService(conn)
    for queue_item_id, finished_at in (
        (1, "2026-08-09 14:32:00"),
        (2, "2026-08-09 18:32:00"),
    ):
        service.record_completed_play(
            station_id=2,
            track_id=1,
            queue_item_id=queue_item_id,
            finished_at=finished_at,
        )

    result = service.export_official_current(destination=tmp_path)

    assert result["integrity"]["valid"] is True
    assert result["integrity"]["record_count"] == 2
    counts = (tmp_path / "RadioTEDU-music-play-counts-current.csv").read_text(
        encoding="utf-8"
    )
    assert "mount" in counts.splitlines()[0]
    assert "mount_status" in counts.splitlines()[0]
    assert "/lofi" in counts
    assert "configured_stream" in counts
    assert "version" in counts.splitlines()[0]
    assert "lyricist" in counts.splitlines()[0]
    assert "phonogram_producer" in counts.splitlines()[0]
    assert "scheduled_duration_seconds" in counts.splitlines()[0]
    assert "TRAAA2600001" in counts
    assert ",2,2," in counts
    rights_report = tmp_path / "RadioTEDU-rights-report-current.csv"
    assert rights_report.is_file()
    assert "Producer" in rights_report.read_text(encoding="utf-8")
    mesam_report = tmp_path / "MESAM" / "current-station-2-radio-form.csv"
    mesam_text = mesam_report.read_text(encoding="utf-8-sig")
    assert mesam_text.splitlines()[0] == "Eser Adı,İcracı,Eser Süresi,Yayın Adedi"
    assert "Test Song,Test Artist,00:03:42,2" in mesam_text
    assert Path(result["manifest"]["path"]).is_file()


def test_licensor_reports_exclude_station_imaging_but_audit_history_keeps_it(
    tmp_path: Path,
) -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO tracks VALUES "
        "(2, 2, 'Station Jingle', 'RadioTEDU-Imaging', 8.0, 'jingle', "
        "'C:/media/jingle.mp3')"
    )
    service = MusicUsageService(conn)
    service.record_completed_play(
        station_id=2,
        track_id=1,
        queue_item_id=1,
        finished_at="2026-08-09 14:32:00",
    )
    service.record_completed_play(
        station_id=2,
        track_id=2,
        queue_item_id=2,
        finished_at="2026-08-09 14:35:00",
    )

    service.export_official_current(destination=tmp_path)

    general_counts = (tmp_path / "RadioTEDU-music-play-counts-current.csv").read_text(
        encoding="utf-8"
    )
    rights_counts = (tmp_path / "RadioTEDU-rights-report-current.csv").read_text(
        encoding="utf-8"
    )
    mesam = (tmp_path / "MESAM" / "current-station-2-radio-form.csv").read_text(
        encoding="utf-8-sig"
    )
    assert "Station Jingle" in general_counts
    assert "Station Jingle" not in rights_counts
    assert "Station Jingle" not in mesam
    assert "Test Song" in rights_counts


def test_internal_report_queries_are_not_truncated_at_ten_thousand_rows():
    conn = _db()
    conn.executemany(
        "INSERT INTO music_usage_log "
        "(station_id, track_id, broadcast_at, work_title, publication_count, "
        "log_id, entry_hash) VALUES (2, 1, '2026-08-09 14:32:00', "
        "'Test Song', 1, ?, ?)",
        ((f"bulk:{index}", f"hash:{index}") for index in range(10001)),
    )
    conn.commit()

    assert len(MusicUsageService(conn).list_entries(limit=10001)) == 10001


def test_integrity_verifier_accepts_immutable_pre_variant_hash_version():
    conn = _db()
    service = MusicUsageService(conn)
    recorded = service.record_completed_play(
        station_id=2,
        track_id=1,
        queue_item_id=1,
        finished_at="2026-08-09 14:32:00",
    )
    legacy_payload = {
        column: recorded.get(column)
        for column in HASH_PAYLOAD_COLUMNS
        if column != "delivered_variants_json"
    }
    legacy_hash = hashlib.sha256(
        json.dumps(
            {**legacy_payload, "previous_hash": ""},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    conn.execute("DROP TRIGGER trg_music_usage_log_no_update")
    conn.execute(
        "UPDATE music_usage_log SET entry_hash=? WHERE id=?",
        (legacy_hash, recorded["id"]),
    )
    conn.commit()

    integrity = service.verify_hash_chain()

    assert integrity["valid"] is True
    assert integrity["hash_versions"]["v1_without_delivered_variants"] == 1


def test_schema_v20_creates_reporting_and_campaign_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "track_broadcast_metadata",
            "music_usage_log",
            "music_usage_month_closures",
            "broadcast_campaigns",
            "broadcast_campaign_stations",
            "genre_voting_rounds",
            "genre_votes",
        } <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == database._SCHEMA_VERSION


def test_schema_v20_repairs_missing_delivered_variants_without_rewriting_usage(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS trg_music_usage_log_no_update")
        conn.execute("ALTER TABLE music_usage_log RENAME TO music_usage_log_complete")
        conn.execute(
            "CREATE TABLE music_usage_log AS SELECT "
            "id, station_id, queue_item_id, track_id, broadcast_at, work_title, version, "
            "performer, composer, lyricist, phonogram_producer, label, isrc, "
            "scheduled_duration_seconds, played_duration_seconds, publication_count, "
            "source_path, source_reference, rights_reference, program_name, presenter, "
            "log_id, metadata_snapshot_json, previous_hash, entry_hash, created_at "
            "FROM music_usage_log_complete"
        )
        conn.execute("DROP TABLE music_usage_log_complete")
        conn.execute(
            "INSERT INTO music_usage_log "
            "(id, station_id, broadcast_at, work_title, log_id, entry_hash) "
            "VALUES (1, 1, '2026-08-12 10:00:00', 'Preserved', 'legacy:1', 'hash-1')"
        )
        conn.execute("PRAGMA user_version=20")
        conn.commit()

    database.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(music_usage_log)")}
        row = conn.execute(
            "SELECT work_title, delivered_variants_json FROM music_usage_log WHERE id=1"
        ).fetchone()
        assert "delivered_variants_json" in columns
        assert row == ("Preserved", "[]")
