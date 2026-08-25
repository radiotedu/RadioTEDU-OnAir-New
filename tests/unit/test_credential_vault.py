import json

import pytest

from app.db import _migrate_station_credentials, get_connection, init_db
from app.repositories.station_output_repo import StationOutputRepository
from app.security.credential_vault import (
    CredentialVault,
    CredentialVaultError,
    credential_protection_scope,
    credential_reference,
    _macos_protect,
    _macos_unprotect,
)


def _protect(value: bytes) -> bytes:
    return b"protected:" + value[::-1]


def _unprotect(value: bytes) -> bytes:
    assert value.startswith(b"protected:")
    return value[len(b"protected:") :][::-1]


def test_vault_never_writes_plaintext(tmp_path):
    path = tmp_path / "vault.json"
    vault = CredentialVault(path, protect=_protect, unprotect=_unprotect)
    reference = credential_reference(4)

    vault.set_secret(reference, "test-stream-password")

    raw = path.read_text(encoding="utf-8")
    assert "test-stream-password" not in raw
    assert vault.get_secret(reference) == "test-stream-password"
    assert vault.has_secret(reference) is True
    assert json.loads(raw)["version"] == 1


def test_vault_rewraps_every_entry_atomically(tmp_path):
    path = tmp_path / "vault.json"
    original = CredentialVault(path, protect=_protect, unprotect=_unprotect)
    first = credential_reference(4)
    second = credential_reference(5)
    original.set_secret(first, "first-password")
    original.set_secret(second, "second-password")

    def replacement_protect(value: bytes) -> bytes:
        return b"replacement:" + value

    migrated = CredentialVault(
        path,
        protect=replacement_protect,
        unprotect=_unprotect,
    )
    assert migrated.rewrap_for_configured_scope() == 2

    def replacement_unprotect(value: bytes) -> bytes:
        assert value.startswith(b"replacement:")
        return value[len(b"replacement:") :]

    verified = CredentialVault(
        path,
        protect=replacement_protect,
        unprotect=replacement_unprotect,
    )
    assert verified.get_secret(first) == "first-password"
    assert verified.get_secret(second) == "second-password"


def test_credential_scope_override_is_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_CREDENTIAL_DPAPI_SCOPE", "machine")
    assert credential_protection_scope(tmp_path / "vault.json") == "machine"

    monkeypatch.setenv("CLEANROOM_CREDENTIAL_DPAPI_SCOPE", "invalid")
    with pytest.raises(CredentialVaultError):
        credential_protection_scope(tmp_path / "vault.json")


def test_legacy_vault_is_rewrapped_once_after_successful_read(tmp_path, monkeypatch):
    path = tmp_path / "vault.json"
    legacy = CredentialVault(path, protect=_protect, unprotect=_unprotect)
    reference = credential_reference(6)
    legacy.set_secret(reference, "legacy-password")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("scope", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    calls = []

    def replacement_protect(value: bytes) -> bytes:
        calls.append(value)
        return _protect(value)

    monkeypatch.setenv("CLEANROOM_CREDENTIAL_DPAPI_SCOPE", "machine")
    migrated = CredentialVault(
        path,
        protect=None,
        unprotect=_unprotect,
    )
    migrated._protect = replacement_protect

    assert migrated.get_secret(reference) == "legacy-password"
    assert calls == [b"legacy-password"]
    assert json.loads(path.read_text(encoding="utf-8"))["scope"] == "machine"

    assert migrated.get_secret(reference) == "legacy-password"
    assert calls == [b"legacy-password"]


def test_station_output_repository_stores_only_credential_reference(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv(
        "CLEANROOM_CREDENTIAL_STORE_FILE", str(tmp_path / "credentials.json")
    )
    init_db()
    conn = get_connection()
    repo = StationOutputRepository(conn)

    repo.upsert(
        station_id=1,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/test",
        icecast_user="source",
        icecast_password="not-for-sqlite",
    )

    raw = repo.get_raw(1)
    assert str(raw["icecast_password"]).startswith("credential://user/")
    assert raw["icecast_password"] != "not-for-sqlite"
    assert repo.get(1)["icecast_password"] == "not-for-sqlite"


def test_schema_migration_moves_legacy_password_and_blanks_settings(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv(
        "CLEANROOM_CREDENTIAL_STORE_FILE", str(tmp_path / "credentials.json")
    )
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO station_outputs (station_id, icecast_password) "
            "VALUES (1, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET icecast_password=excluded.icecast_password",
            ("legacy-password",),
        )
        conn.execute(
            "INSERT INTO station_settings (station_id, key, value) "
            "VALUES (1, 'icecast_password', ?) "
            "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value",
            ("legacy-settings-password",),
        )

        _migrate_station_credentials(conn.cursor())
        conn.commit()

        raw = StationOutputRepository(conn).get_raw(1)
        assert str(raw["icecast_password"]).startswith("credential://user/station/1/")
        assert StationOutputRepository(conn).get(1)["icecast_password"] == "legacy-password"
        row = conn.execute(
            "SELECT value FROM station_settings "
            "WHERE station_id=1 AND key='icecast_password'"
        ).fetchone()
        assert row["value"] == ""
    finally:
        conn.close()


def test_credentials_can_be_exported_and_reprotected_on_a_recovery_host(tmp_path):
    source = CredentialVault(tmp_path / "source.json", protect=_protect, unprotect=_unprotect)
    target = CredentialVault(tmp_path / "target.json", protect=_protect, unprotect=_unprotect)
    reference = credential_reference(22)
    source.set_secret(reference, "portable-password")

    exported = source.export_secrets()
    assert exported == {reference: "portable-password"}
    assert target.import_secrets(exported) == 1
    assert target.get_secret(reference) == "portable-password"


def test_recovery_import_commits_all_credentials_in_one_atomic_write(tmp_path, monkeypatch):
    path = tmp_path / "target.json"
    target = CredentialVault(path, protect=_protect, unprotect=_unprotect)
    first = credential_reference(31)
    second = credential_reference(32)
    writes = 0
    original_write = target._write

    def counted_write(payload):
        nonlocal writes
        writes += 1
        original_write(payload)

    monkeypatch.setattr(target, "_write", counted_write)
    assert target.import_secrets({first: "alpha", second: "beta"}) == 2
    assert writes == 1
    assert target.get_secret(first) == "alpha"
    assert target.get_secret(second) == "beta"


def test_recovery_import_protection_failure_leaves_vault_unchanged(tmp_path):
    path = tmp_path / "target.json"
    existing = credential_reference(40)
    original = CredentialVault(path, protect=_protect, unprotect=_unprotect)
    original.set_secret(existing, "preserved")
    before = path.read_bytes()

    calls = 0

    def failing_protect(value: bytes) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated DPAPI failure")
        return _protect(value)

    target = CredentialVault(path, protect=failing_protect, unprotect=_unprotect)
    with pytest.raises(OSError, match="simulated DPAPI failure"):
        target.import_secrets(
            {
                credential_reference(41): "first",
                credential_reference(42): "second",
            }
        )

    assert path.read_bytes() == before
    assert original.get_secret(existing) == "preserved"


def test_macos_keychain_backed_payload_round_trip(monkeypatch):
    monkeypatch.setattr(
        "app.security.credential_vault._macos_master_key",
        lambda: bytes(range(32)),
    )

    protected = _macos_protect(b"portable-stream-password")

    assert b"portable-stream-password" not in protected
    assert _macos_unprotect(protected) == b"portable-stream-password"


def test_macos_payload_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(
        "app.security.credential_vault._macos_master_key",
        lambda: bytes(range(32)),
    )
    protected = _macos_protect(b"portable-stream-password")
    monkeypatch.setattr(
        "app.security.credential_vault._macos_master_key",
        lambda: bytes(reversed(range(32))),
    )

    with pytest.raises(CredentialVaultError, match="could not be decrypted"):
        _macos_unprotect(protected)
