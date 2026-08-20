from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools import backup_onair_state as backup


def test_backup_snapshot_is_atomic_when_a_protected_file_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_root = tmp_path / "OnAir"
    recovery_root = live_root / "Recovery"
    recovery_root.mkdir(parents=True)
    with sqlite3.connect(live_root / "cleanroom.db") as conn:
        conn.execute("CREATE TABLE canary(value TEXT)")
        conn.commit()

    monkeypatch.setattr(backup, "PRIMARY_ROOT", live_root)
    monkeypatch.setattr(backup, "SOURCE_ROOTS", (("programdata-onair", live_root),))
    monkeypatch.setattr(backup, "EXPLICIT_FILES", (("protected", live_root / "protected.env"),))
    (live_root / "protected.env").write_text("secret", encoding="utf-8")

    def _permission_denied(*_args, **_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(backup, "_ensure_readable", _permission_denied)
    destination = recovery_root / "atomic-fixture"
    monkeypatch.setattr("sys.argv", ["backup_onair_state.py", "--destination", str(destination)])

    with pytest.raises(PermissionError, match="elevated account"):
        backup.main()

    assert not destination.exists()
    assert not list(recovery_root.glob(".atomic-fixture.staging-*"))
