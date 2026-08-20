from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_data_root, get_db_path
from app.db import get_connection, init_db
from app.runtime_paths import get_data_dir
from app.security.credential_vault import protect_data, unprotect_data

_HEADER = b"ONAIR-DPAPI-1\n"
_RETENTION = {"hourly": 48, "daily": 30, "monthly": 12}
_MINIMUM_FREE_BYTES = 512 * 1024 * 1024
_log = logging.getLogger("cleanroom.recovery_points")


class RecoveryPointService:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return get_data_dir() / "recovery-points"

    def _backup_sqlite(self, target: Path) -> None:
        source = sqlite3.connect(str(get_db_path()), timeout=30)
        destination = sqlite3.connect(str(target), timeout=30)
        try:
            source.backup(destination)
            row = destination.execute("PRAGMA quick_check(1)").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise RuntimeError("backup_integrity_failed")
            if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("backup_foreign_key_check_failed")
        finally:
            destination.close()
            source.close()

    def _ensure_capacity(self, directory: Path) -> None:
        database = get_db_path()
        source_bytes = 0
        for path in (
            database,
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
        ):
            try:
                source_bytes += int(path.stat().st_size)
            except OSError:
                continue
        required = max(_MINIMUM_FREE_BYTES, source_bytes * 3)
        if int(shutil.disk_usage(directory).free) < required:
            raise RuntimeError("insufficient_disk_space_for_recovery_point")

    def create(self, tier: str = "hourly") -> dict:
        normalized = str(tier).lower()
        if normalized not in _RETENTION:
            raise ValueError("invalid_recovery_tier")
        init_db()
        with self._lock:
            now = datetime.now(timezone.utc)
            directory = self.root / normalized
            directory.mkdir(parents=True, exist_ok=True)
            self._ensure_capacity(directory)
            name = (
                now.strftime("%Y%m%dT%H%M%S%fZ")
                + f"-{os.urandom(4).hex()}.db.dpapi"
            )
            final_path = directory / name
            fd, temporary_name = tempfile.mkstemp(prefix="onair-backup-", suffix=".db", dir=str(directory))
            os.close(fd)
            temporary = Path(temporary_name)
            encrypted_temporary = final_path.with_name(f".{final_path.name}.tmp")
            published = False
            try:
                self._backup_sqlite(temporary)
                raw = temporary.read_bytes()
                protected = protect_data(raw) if os.name == "nt" else raw
                with encrypted_temporary.open("xb") as handle:
                    handle.write(
                        (_HEADER if os.name == "nt" else b"ONAIR-PLAIN-TEST\n")
                        + protected
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    encrypted_temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(encrypted_temporary, final_path)
                verification = self.verify_restore(final_path)
                if not bool(verification.get("valid")):
                    raise RuntimeError("backup_restore_verification_failed")
                digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
                conn = get_connection()
                try:
                    conn.execute(
                        "INSERT INTO recovery_points(tier, file_path, sha256, size_bytes, integrity_status, verified_at) "
                        "VALUES (?, ?, ?, ?, 'ok', CURRENT_TIMESTAMP)",
                        (normalized, str(final_path), digest, final_path.stat().st_size),
                    )
                    conn.commit()
                finally:
                    conn.close()
                published = True
                self._prune(normalized)
                return {"tier": normalized, "file_path": str(final_path), "sha256": digest, "verified": True}
            finally:
                temporary.unlink(missing_ok=True)
                encrypted_temporary.unlink(missing_ok=True)
                if not published:
                    final_path.unlink(missing_ok=True)

    def verify_restore(
        self, path: str | Path, *, expected_sha256: str | None = None
    ) -> dict:
        source = Path(path).resolve()
        if not source.is_relative_to(self.root.resolve()):
            raise RuntimeError("invalid_recovery_path")
        try:
            database = self._read_recovery_database(
                source, expected_sha256=expected_sha256
            )
        except RuntimeError as exc:
            if str(exc) == "recovery_digest_mismatch":
                return {"valid": False, "path": str(source)}
            raise
        valid = self._verify_database_bytes(database)
        return {"valid": valid, "path": str(source)}

    @staticmethod
    def _verify_database_bytes(database: bytes) -> bool:
        fd, name = tempfile.mkstemp(prefix="onair-restore-test-", suffix=".db")
        os.close(fd)
        temporary = Path(name)
        try:
            temporary.write_bytes(database)
            conn = sqlite3.connect(str(temporary))
            try:
                row = conn.execute("PRAGMA quick_check(1)").fetchone()
                return bool(
                    row
                    and str(row[0]).lower() == "ok"
                    and conn.execute("PRAGMA foreign_key_check").fetchone() is None
                )
            finally:
                conn.close()
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_recovery_database(
        source: Path, *, expected_sha256: str | None = None
    ) -> bytes:
        raw = source.read_bytes()
        if expected_sha256:
            actual_sha256 = hashlib.sha256(raw).hexdigest()
            if actual_sha256.lower() != str(expected_sha256).strip().lower():
                raise RuntimeError("recovery_digest_mismatch")
        if raw.startswith(_HEADER):
            return unprotect_data(raw[len(_HEADER) :])
        elif raw.startswith(b"ONAIR-PLAIN-TEST\n"):
            return raw[len(b"ONAIR-PLAIN-TEST\n") :]
        raise RuntimeError("unknown_recovery_format")

    def stage_restore(
        self, path: str | Path, *, expected_sha256: str
    ) -> dict[str, object]:
        """Stage a verified database for the supervisor's offline restore path.

        The active database is never replaced here. The supervisor consumes the
        atomic pending plan before its next backend start, after the active
        backend has exited, and retains the pre-restore database for rollback.
        """
        source = Path(path).resolve()
        if not source.is_relative_to(self.root.resolve()):
            raise RuntimeError("invalid_recovery_path")
        data_root = get_data_root().resolve()
        target_database = get_db_path().resolve()
        if not target_database.is_relative_to(data_root):
            raise RuntimeError("recovery_target_outside_data_root")

        with self._lock:
            pending_path = data_root / "State" / "Recovery" / "pending.json"
            if pending_path.exists():
                raise RuntimeError("recovery_plan_already_pending")

            database = self._read_recovery_database(
                source, expected_sha256=expected_sha256
            )
            if not self._verify_database_bytes(database):
                raise RuntimeError("recovery_database_invalid")

            self._ensure_capacity(data_root)
            plan_id = str(uuid.uuid4())
            staging_root = data_root / "Recovery" / "Staging" / plan_id
            staged_database = staging_root / "database.db"
            staged_temporary = staging_root / ".database.db.tmp"
            backup_database = data_root / "Backups" / f"{plan_id}-database.bak"
            plan_temporary = pending_path.with_name(f".{plan_id}.pending.tmp")
            published = False
            try:
                staging_root.mkdir(parents=True, exist_ok=False)
                with staged_temporary.open("xb") as handle:
                    handle.write(database)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staged_temporary, staged_database)
                staged_sha256 = hashlib.sha256(staged_database.read_bytes()).hexdigest()

                plan = {
                    "schema": 1,
                    "planId": plan_id,
                    "sourceDatabase": str(staged_database),
                    "sourceDatabaseSha256": staged_sha256,
                    "targetDatabase": str(target_database),
                    "backupDatabase": str(backup_database),
                    "sourceCredentialVault": None,
                    "sourceCredentialVaultSha256": None,
                    "targetCredentialVault": None,
                    "backupCredentialVault": None,
                    "deleteCredentialTarget": False,
                    "originPlanId": None,
                }
                pending_path.parent.mkdir(parents=True, exist_ok=True)
                with plan_temporary.open("x", encoding="utf-8") as handle:
                    json.dump(plan, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(plan_temporary, pending_path)
                published = True
                return {
                    "plan_id": plan_id,
                    "restart_required": True,
                    "staged": True,
                }
            finally:
                staged_temporary.unlink(missing_ok=True)
                plan_temporary.unlink(missing_ok=True)
                if not published:
                    shutil.rmtree(staging_root, ignore_errors=True)

    def _prune(self, tier: str) -> None:
        files = sorted((self.root / tier).glob("*.db.dpapi"), key=lambda p: p.stat().st_mtime, reverse=True)
        stale_paths = set()
        for stale in files[_RETENTION[tier] :]:
            try:
                stale.unlink(missing_ok=True)
                stale_paths.add(str(stale))
            except OSError:
                continue
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, file_path FROM recovery_points WHERE tier=?",
                (tier,),
            ).fetchall()
            stale_ids = [
                int(row["id"])
                for row in rows
                if str(row["file_path"] or "") in stale_paths
                or not Path(str(row["file_path"] or "")).is_file()
            ]
            if stale_ids:
                conn.executemany(
                    "DELETE FROM recovery_points WHERE id=?",
                    [(point_id,) for point_id in stale_ids],
                )
                conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _period_key(tier: str, value: str) -> str:
        normalized = str(value or "").replace("T", " ")
        lengths = {"hourly": 13, "daily": 10, "monthly": 7}
        return normalized[: lengths[tier]]

    def _due_tiers(self, now: datetime) -> list[str]:
        init_db()
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT tier, file_path, created_at FROM recovery_points "
                "WHERE integrity_status='ok' ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
        latest = {}
        for row in rows:
            tier = str(row["tier"] or "")
            if tier in latest or tier not in _RETENTION:
                continue
            if not Path(str(row["file_path"] or "")).is_file():
                continue
            latest[tier] = self._period_key(tier, str(row["created_at"] or ""))
        timestamp = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return [
            tier
            for tier in ("hourly", "daily", "monthly")
            if latest.get(tier) != self._period_key(tier, timestamp)
        ]

    def start(self) -> None:
        if self._thread is not None or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("CLEANROOM_DISABLE_RECOVERY_POINTS", "").lower() in {"1", "true", "yes"}:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="onair-recovery-points", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(5):
            now = datetime.now(timezone.utc)
            for tier in self._due_tiers(now):
                try:
                    self.create(tier)
                except Exception as exc:
                    _log.warning(
                        "Scheduled recovery point failed tier=%s code=%s",
                        tier,
                        type(exc).__name__,
                    )
            self._stop.wait(3600)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None


recovery_point_service = RecoveryPointService()
