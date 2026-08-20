from datetime import datetime, timezone
import os
from types import SimpleNamespace

import pytest

from app.db import get_connection, init_db
from app.services.recovery_points import RecoveryPointService


def _configure_data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(root / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(tmp_path / "user"))
    init_db()
    return root


def test_scheduler_uses_persisted_periods_across_backend_restart(
    tmp_path,
    monkeypatch,
):
    _configure_data_root(tmp_path, monkeypatch)
    service = RecoveryPointService()
    timestamp = "2026-08-09 10:05:00"
    conn = get_connection()
    try:
        for tier in ("hourly", "daily", "monthly"):
            path = service.root / tier / f"{tier}.db.dpapi"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"backup")
            conn.execute(
                "INSERT INTO recovery_points"
                "(tier, file_path, sha256, size_bytes, integrity_status, created_at, verified_at) "
                "VALUES (?, ?, 'digest', 6, 'ok', ?, ?)",
                (tier, str(path), timestamp, timestamp),
            )
        conn.commit()
    finally:
        conn.close()

    assert service._due_tiers(
        datetime(2026, 8, 9, 10, 50, tzinfo=timezone.utc)
    ) == []
    assert service._due_tiers(
        datetime(2026, 8, 9, 11, 1, tzinfo=timezone.utc)
    ) == ["hourly"]


def test_recovery_retention_prunes_files_and_database_rows(tmp_path, monkeypatch):
    _configure_data_root(tmp_path, monkeypatch)
    service = RecoveryPointService()
    directory = service.root / "hourly"
    directory.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        for index in range(50):
            path = directory / f"point-{index:02d}.db.dpapi"
            path.write_bytes(b"backup")
            os.utime(path, (index + 1, index + 1))
            conn.execute(
                "INSERT INTO recovery_points"
                "(tier, file_path, sha256, size_bytes, integrity_status, verified_at) "
                "VALUES ('hourly', ?, 'digest', 6, 'ok', CURRENT_TIMESTAMP)",
                (str(path),),
            )
        conn.commit()
    finally:
        conn.close()

    service._prune("hourly")

    assert len(list(directory.glob("*.db.dpapi"))) == 48
    conn = get_connection()
    try:
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM recovery_points WHERE tier='hourly'"
            ).fetchone()[0]
        ) == 48
    finally:
        conn.close()


def test_recovery_point_refuses_to_consume_last_disk_reserve(
    tmp_path,
    monkeypatch,
):
    _configure_data_root(tmp_path, monkeypatch)
    service = RecoveryPointService()
    directory = service.root / "hourly"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "app.services.recovery_points.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1024, used=1023, free=1),
    )

    with pytest.raises(RuntimeError, match="insufficient_disk_space"):
        service._ensure_capacity(directory)
