from __future__ import annotations

import json
import sqlite3

from app.services.codec_migration import migrate_ogg_outputs_to_he_aac


def test_codec_policy_migration_changes_normal_and_low_but_not_flac() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE station_outputs (
            station_id INTEGER PRIMARY KEY,
            icecast_mount TEXT NOT NULL,
            stream_codec_profile TEXT NOT NULL,
            stream_bitrate_kbps INTEGER NOT NULL
        );
        CREATE TABLE station_settings (
            station_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (station_id, key)
        );
        """
    )
    conn.execute(
        "INSERT INTO station_outputs VALUES (1, '/radio', 'he_aac_192', 192)"
    )
    conn.executemany(
        "INSERT INTO station_settings(station_id, key, value) VALUES (1, ?, ?)",
        (("stream_codec_profile", "he_aac_192"), ("stream_bitrate_kbps", "192")),
    )
    outputs = [
        {
            "quality": "low",
            "icecast_mount": "/radio-low",
            "stream_codec_profile": "he_aac_96",
            "stream_bitrate_kbps": 96,
        },
        {
            "quality": "flac",
            "icecast_mount": "/classic-flac",
            "stream_codec_profile": "ogg_flac_lossless",
            "stream_bitrate_kbps": 0,
        },
    ]
    conn.execute(
        "INSERT INTO system_settings(key, value) VALUES (?, ?)",
        ("station_1_extra_icecast_outputs", json.dumps(outputs)),
    )
    conn.commit()

    result = migrate_ogg_outputs_to_he_aac(conn)

    primary = conn.execute(
        "SELECT stream_codec_profile, stream_bitrate_kbps FROM station_outputs"
    ).fetchone()
    stored = json.loads(
        conn.execute(
            "SELECT value FROM system_settings "
            "WHERE key='station_1_extra_icecast_outputs'"
        ).fetchone()[0]
    )
    assert result["primary_changed"] == 1
    assert result["extra_changed"] == 1
    assert primary == ("aac_low_192", 192)
    assert stored[0]["stream_codec_profile"] == "aac_he_v2_64"
    assert stored[0]["stream_bitrate_kbps"] == 64
    assert stored[1]["stream_codec_profile"] == "ogg_flac_lossless"
    assert stored[1]["stream_bitrate_kbps"] == 0

    second = migrate_ogg_outputs_to_he_aac(conn)
    assert second["already_applied"] is True
