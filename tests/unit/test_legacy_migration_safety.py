import sqlite3
from pathlib import Path

import app.migration.legacy_import as legacy_import


def _fake_import(
    _source: Path,
    target: Path,
    *,
    copy_media: bool,
    media_destination_root,
    legacy_repo_root=None,
) -> dict:
    Path(target).write_bytes(b"validated-new-database")
    return {
        "stations": 2,
        "tracks": 12,
        "media_copied": int(bool(copy_media)),
        "warnings": [],
    }


def _make_source(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def test_dry_run_validates_staging_without_touching_target(
    tmp_path, monkeypatch
):
    source = tmp_path / "legacy.db"
    target = tmp_path / "onair.db"
    _make_source(source)
    target.write_bytes(b"existing-database")
    monkeypatch.setattr(legacy_import, "import_legacy_database", _fake_import)
    monkeypatch.setenv(
        "CLEANROOM_CREDENTIAL_STORE_FILE",
        str(tmp_path / "operator-vault.json"),
    )

    report = legacy_import.migrate_legacy_database_safely(
        source,
        target,
        dry_run=True,
        copy_media=True,
    )

    assert target.read_bytes() == b"existing-database"
    assert report["dry_run"] is True
    assert report["target_replaced"] is False
    assert report["media_copied"] == 0
    assert not list(tmp_path.glob("*.backup-*"))


def test_validated_migration_backs_up_then_atomically_replaces_target(
    tmp_path, monkeypatch
):
    source = tmp_path / "legacy.db"
    target = tmp_path / "onair.db"
    _make_source(source)
    target.write_bytes(b"existing-database")
    monkeypatch.setattr(legacy_import, "import_legacy_database", _fake_import)

    report = legacy_import.migrate_legacy_database_safely(
        source,
        target,
        dry_run=False,
    )

    backup = Path(report["backup_database"])
    assert report["target_replaced"] is True
    assert target.read_bytes() == b"validated-new-database"
    assert backup.read_bytes() == b"existing-database"
