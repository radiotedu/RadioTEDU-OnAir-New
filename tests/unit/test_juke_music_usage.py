from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.services.juke_music_usage import (
    JukeLedgerIntegrityError,
    discover_juke_ledger_path,
    list_juke_music_usage,
)


def _connection(tmp_path, ledger_path):
    env_path = tmp_path / "juke.env"
    env_path.write_text(
        f"MEDIA_AGENT_REQUEST_SECRET=must-never-be-returned\nJUKE_PLAY_LEDGER_PATH={ledger_path}\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO system_settings VALUES (?, ?)",
        (
            "radiotedu_service_control_v1",
            json.dumps({"juke_media_agent": {"config_path": str(env_path)}}),
        ),
    )
    return conn


def _append(ledger_path, payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row = {
        **payload,
        "canonical_payload": canonical,
        "entry_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    with ledger_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def test_discovers_only_safe_path_keys_and_verifies_records(tmp_path):
    ledger_path = tmp_path / "music-usage.jsonl"
    conn = _connection(tmp_path, ledger_path)
    first = _append(
        ledger_path,
        {
            "version": 1,
            "event_id": "event-0000000001",
            "broadcast_at": "2026-08-11T10:03:38.000Z",
            "station_id": "juke-local",
            "work_title": "Flowers",
            "version_name": "Radio edit",
            "performer": "Kevin McLoud",
            "composer": "",
            "lyricist": "",
            "phonogram_producer": "",
            "label": "",
            "isrc": "",
            "scheduled_duration_seconds": 222,
            "played_duration_seconds": 218,
            "delivery_duration_seconds": 1,
            "publication_count": 1,
            "source_path": "overflow/Kevin McLoud - Flowers.flac",
            "source_reference": "media://overflow/Kevin McLoud - Flowers.flac",
            "rights_reference": "",
            "program_name": "Juke Local",
            "presenter": "automation",
            "log_id": "juke:event-0000000001",
            "size_bytes": 100,
            "delivered_bytes": 100,
            "previous_hash": "",
        },
    )
    items, status = list_juke_music_usage(conn)
    assert discover_juke_ledger_path(conn) == ledger_path.resolve()
    assert status == {
        "configured": True,
        "available": True,
        "integrity_ok": True,
        "record_count": 1,
        "last_recorded_at": "2026-08-11T10:03:38.000Z",
    }
    assert items[0]["work_title"] == "Flowers"
    assert items[0]["source_system"] == "juke_local"
    assert items[0]["entry_hash"] == first["entry_hash"]
    assert "must-never-be-returned" not in json.dumps(items)


def test_rejects_payload_or_hash_chain_tampering(tmp_path):
    ledger_path = tmp_path / "music-usage.jsonl"
    conn = _connection(tmp_path, ledger_path)
    row = _append(
        ledger_path,
        {
            "version": 1,
            "event_id": "event-0000000002",
            "broadcast_at": "2026-08-11T10:03:38.000Z",
            "previous_hash": "",
            "work_title": "Original",
        },
    )
    row["work_title"] = "Changed"
    ledger_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(JukeLedgerIntegrityError, match="payload_mismatch"):
        list_juke_music_usage(conn)
