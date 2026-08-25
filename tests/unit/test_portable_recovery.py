from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from cryptography.exceptions import InvalidTag

from tools.import_portable_recovery import _copy_database, _mapped_path
from tools.portable_recovery_crypto import decrypt_json, encrypt_json


def test_portable_recovery_secret_round_trip_does_not_store_plaintext():
    payload = {
        "credential_values": {
            "credential://user/station/1/icecast": "stream-password"
        }
    }

    encrypted = encrypt_json(payload, "archive-password")

    assert b"stream-password" not in encrypted
    assert decrypt_json(encrypted, "archive-password") == payload


def test_portable_recovery_secret_rejects_wrong_password():
    encrypted = encrypt_json({"value": "secret"}, "correct")

    with pytest.raises(InvalidTag):
        decrypt_json(encrypted, "incorrect")


def test_windows_media_path_maps_to_macos_volume(tmp_path: Path):
    media_root = tmp_path / "RadioTEDU Media"

    mapped = _mapped_path(
        r"H:\RadioTEDU Live\Classical\track.flac",
        "H:",
        media_root,
    )

    assert mapped == str(
        (media_root / "RadioTEDU Live" / "Classical" / "track.flac").resolve()
    )
    assert _mapped_path(r"C:\other\track.flac", "H:", media_root) is None


def test_portable_database_restore_closes_snapshot_before_atomic_replace(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "restored" / "cleanroom.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE canary(value TEXT)")
        conn.execute("INSERT INTO canary(value) VALUES ('ready')")

    assert _copy_database(source, target) is None

    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT value FROM canary").fetchone()[0] == "ready"
