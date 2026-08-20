from __future__ import annotations

import json
import sqlite3
import unittest

from app.services.music_usage import MusicUsageService


class QualityComplianceVariantsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY, station_id INTEGER, title TEXT, artist TEXT,
                duration REAL, track_type TEXT, file_path TEXT
            );
            CREATE TABLE track_broadcast_metadata (
                track_id INTEGER PRIMARY KEY, version TEXT DEFAULT '', composer TEXT DEFAULT '',
                lyricist TEXT DEFAULT '', phonogram_producer TEXT DEFAULT '', label TEXT DEFAULT '',
                isrc TEXT DEFAULT '', source_reference TEXT DEFAULT '', rights_reference TEXT DEFAULT ''
            );
            CREATE TABLE music_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER, queue_item_id INTEGER,
                track_id INTEGER, broadcast_at TEXT, work_title TEXT, version TEXT, performer TEXT,
                composer TEXT, lyricist TEXT, phonogram_producer TEXT, label TEXT, isrc TEXT,
                scheduled_duration_seconds REAL, played_duration_seconds REAL, publication_count INTEGER,
                source_path TEXT, source_reference TEXT, rights_reference TEXT, program_name TEXT,
                presenter TEXT, delivered_variants_json TEXT DEFAULT '[]', log_id TEXT UNIQUE,
                metadata_snapshot_json TEXT, previous_hash TEXT, entry_hash TEXT UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO tracks VALUES (
                1, 2, 'Test Song', 'Test Artist', 222.0, 'music', 'C:/media/test.mp3'
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_approved_deliveries_are_one_broadcast_with_a_verified_variant_snapshot(self):
        variants = [
            {
                "mount": mount,
                "quality": quality,
                "codec_profile": profile,
                "bitrate_kbps": bitrate,
            }
            for mount, quality, profile, bitrate in (
                ("/classic", "normal", "opus_192", 192),
                ("/classic-low", "low", "opus_32", 32),
                ("/classic-flac", "flac", "ogg_flac_lossless", 0),
            )
        ]
        service = MusicUsageService(self.conn)

        first = service.record_completed_play(
            station_id=2,
            track_id=1,
            queue_item_id=55,
            finished_at="2026-08-11 12:00:00",
            delivered_variants=variants,
        )
        duplicate = service.record_completed_play(
            station_id=2,
            track_id=1,
            queue_item_id=55,
            finished_at="2026-08-11 12:00:00",
            delivered_variants=variants,
        )

        delivered = json.loads(first["delivered_variants_json"])
        snapshot = json.loads(first["metadata_snapshot_json"])
        self.assertEqual(first["publication_count"], 1)
        self.assertEqual(len(delivered), 3)
        self.assertEqual(snapshot["delivered_variants"], delivered)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM music_usage_log").fetchone()[0],
            1,
        )
        self.assertIn("delivered_variants_json", service.csv_text([dict(first)]).splitlines()[0])


if __name__ == "__main__":
    unittest.main()
