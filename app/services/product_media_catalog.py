"""Durable, independent catalogs for the fixed RadioTEDU product folders.

This deliberately does not share the legacy station-library watcher: each
product receives an isolated SQLite catalog and an all-or-nothing generation.
The service only records files that are stable, local, supported audio below
the configured media root; failed or unstable scans leave the last generation
untouched.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.file_security import audio_upload_extensions


DEFAULT_MEDIA_ROOT = Path("E:/RadioTEDU Media")
PRODUCT_DIRECTORIES = {
    "broadcast": "Broadcast",
    "juke": "Juke/Non-Turkish",
    "voting": "Voting",
    "jingles": "Jingles",
    "ads": "Ads",
    "emergency": "Emergency",
}
_REPARSE_POINT = 0x0400


class ProductCatalogError(ValueError):
    """A stable, operator-safe product catalog failure code."""


@dataclass(frozen=True)
class _MediaFile:
    relative_path: str
    size: int
    modified_ns: int


@dataclass
class _ProductState:
    fingerprint: str = ""
    stable_polls: int = 0
    synced_fingerprint: str = ""
    status: str = "boot_reconcile"
    file_count: int = 0
    last_scan_at: float = 0.0
    last_sync_at: float = 0.0
    generation: int = 0
    retry_count: int = 0
    next_retry_at: float = 0.0
    error_code: str = ""
    last_good_generation: int = 0
    force_rescan: bool = False


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else ""


def _configured_seconds(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


class ProductMediaCatalogService:
    """Poll fixed product folders and commit only settled catalog generations."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        poll_interval_seconds: float | None = None,
        required_stable_polls: int = 2,
        minimum_quiet_seconds: float = 8.0,
        max_retries: int = 4,
    ):
        configured = str(root or os.getenv("RADIOTEDU_MEDIA_ROOT", "")).strip()
        configured_root = Path(configured) if configured else DEFAULT_MEDIA_ROOT
        if not configured_root.is_absolute():
            raise ProductCatalogError("media_root_must_be_absolute")
        configured_root = configured_root.expanduser()
        if self._exists_static(configured_root) and self._reparse_or_symlink_static(
            self._io_path(configured_root)
        ):
            raise ProductCatalogError("media_root_unsafe")
        self.root = configured_root.resolve(strict=False)
        self._poll_interval = (
            max(0.2, float(poll_interval_seconds))
            if poll_interval_seconds is not None
            else _configured_seconds(
                "RADIOTEDU_PRODUCT_CATALOG_POLL_SECONDS", 60.0, minimum=15.0, maximum=300.0
            )
        )
        self._stable_polls = max(2, int(required_stable_polls))
        self._quiet_seconds = max(0.0, float(minimum_quiet_seconds))
        self._max_retries = max(0, int(max_retries))
        self._states = {product: _ProductState() for product in PRODUCT_DIRECTORIES}
        self._state_lock = threading.RLock()
        self._product_locks = {product: threading.Lock() for product in PRODUCT_DIRECTORIES}
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _io_path(path: Path | str) -> str:
        raw = os.fspath(path)
        if os.name != "nt" or raw.startswith("\\\\?\\"):
            return raw
        normalized = ntpath.normpath(ntpath.abspath(raw))
        if normalized.startswith("\\\\"):
            return "\\\\?\\UNC\\" + normalized[2:]
        return "\\\\?\\" + normalized

    @staticmethod
    def _exists_static(path: Path | str) -> bool:
        return os.path.exists(ProductMediaCatalogService._io_path(path))

    @staticmethod
    def _reparse_or_symlink_static(path: Path | str) -> bool:
        try:
            info = os.lstat(os.fspath(path))
        except OSError as exc:
            raise ProductCatalogError("media_path_not_accessible") from exc
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)

    def _folder(self, product: str) -> Path:
        return self.root / Path(PRODUCT_DIRECTORIES[product])

    def _database(self, product: str) -> Path:
        return self.root / "Databases" / f"{product}.sqlite3"

    @staticmethod
    def _reparse_or_symlink(path: Path | str) -> bool:
        return ProductMediaCatalogService._reparse_or_symlink_static(path)

    def _scan(self, product: str, now: float) -> tuple[str, list[_MediaFile], bool]:
        folder = self._folder(product)
        folder_io = self._io_path(folder)
        try:
            folder_stat = os.stat(folder_io)
        except OSError as exc:
            raise ProductCatalogError("product_folder_not_ready") from exc
        if not stat.S_ISDIR(folder_stat.st_mode):
            raise ProductCatalogError("product_folder_not_ready")
        if self._reparse_or_symlink(folder_io):
            raise ProductCatalogError("media_path_unsafe")

        extensions = audio_upload_extensions()
        records: list[_MediaFile] = []
        casefolded: set[str] = set()
        quiet = True
        try:
            for current, directories, filenames in os.walk(folder_io, topdown=True, followlinks=False):
                directories.sort()
                filenames.sort()
                for directory in directories:
                    if self._reparse_or_symlink(os.path.join(current, directory)):
                        raise ProductCatalogError("media_path_unsafe")
                for filename in filenames:
                    candidate = os.path.join(current, filename)
                    if self._reparse_or_symlink(candidate):
                        raise ProductCatalogError("media_path_unsafe")
                    file_stat = os.stat(candidate)
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                    logical_relative = Path(os.path.relpath(candidate, folder_io)).as_posix()
                    if logical_relative == "." or logical_relative.startswith("../"):
                        raise ProductCatalogError("media_path_unsafe")
                    if Path(logical_relative).suffix.lower() not in extensions:
                        continue
                    folded = logical_relative.casefold()
                    if folded in casefolded:
                        raise ProductCatalogError("media_path_case_collision")
                    casefolded.add(folded)
                    if now - (file_stat.st_mtime_ns / 1_000_000_000) < self._quiet_seconds:
                        quiet = False
                    records.append(_MediaFile(logical_relative, int(file_stat.st_size), int(file_stat.st_mtime_ns)))
        except ProductCatalogError:
            raise
        except OSError as exc:
            raise ProductCatalogError("media_scan_failed") from exc
        records.sort(key=lambda item: item.relative_path.casefold())
        digest = hashlib.sha256()
        for item in records:
            digest.update(item.relative_path.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(item.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(item.modified_ns).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest(), records, quiet

    def _assert_catalog_path(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ProductCatalogError("catalog_path_unsafe") from exc
        if self._exists(path) and self._reparse_or_symlink(self._io_path(path)):
            raise ProductCatalogError("catalog_path_unsafe")

    def _prepare_catalog_paths(self, product: str) -> Path:
        database = self._database(product)
        self._assert_catalog_path(self.root)
        self._assert_catalog_path(database.parent)
        try:
            os.makedirs(self._io_path(database.parent), exist_ok=True)
        except OSError as exc:
            raise ProductCatalogError("catalog_path_not_ready") from exc
        self._assert_catalog_path(database.parent)
        for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            self._assert_catalog_path(candidate)
        return database

    @contextmanager
    def _product_file_lock(self, product: str):
        lock_root = self.root / "Databases" / ".catalog-locks"
        lock_path = lock_root / f"{product}.lock"
        self._assert_catalog_path(lock_root.parent)
        self._assert_catalog_path(lock_root)
        try:
            os.makedirs(self._io_path(lock_root), exist_ok=True)
        except OSError as exc:
            raise ProductCatalogError("catalog_lock_unavailable") from exc
        self._assert_catalog_path(lock_root)
        self._assert_catalog_path(lock_path)
        try:
            handle = open(self._io_path(lock_path), "a+b")
        except OSError as exc:
            raise ProductCatalogError("catalog_lock_unavailable") from exc
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield
        except ProductCatalogError:
            raise
        except OSError as exc:
            raise ProductCatalogError("catalog_lock_busy") from exc
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _load_existing_catalog(self, product: str) -> tuple[int, str]:
        database = self._prepare_catalog_paths(product)
        if not self._exists(database):
            return 0, ""
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._io_path(database), timeout=5.0)
            connection.execute("PRAGMA busy_timeout=5000")
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ProductCatalogError("catalog_integrity_failed")
            row = connection.execute("SELECT generation, fingerprint FROM catalog_meta WHERE id=1").fetchone()
            return (int(row[0]), str(row[1])) if row else (0, "")
        except ProductCatalogError:
            raise
        except sqlite3.Error as exc:
            raise ProductCatalogError("catalog_boot_check_failed") from exc
        finally:
            if connection is not None:
                connection.close()

    def _write_generation(self, product: str, fingerprint: str, records: list[_MediaFile]) -> int:
        """Lock, revalidate, and atomically replace a single product generation."""
        with self._product_file_lock(product):
            current_fingerprint, current_records, quiet = self._scan(product, time.time())
            if not quiet or current_fingerprint != fingerprint:
                raise ProductCatalogError("catalog_snapshot_changed")
            database = self._prepare_catalog_paths(product)
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(self._io_path(database), timeout=5.0, isolation_level=None)
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
                    self._assert_catalog_path(candidate)
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS catalog_meta (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, fingerprint TEXT NOT NULL, committed_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS catalog_items (relative_path TEXT PRIMARY KEY COLLATE NOCASE, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, generation INTEGER NOT NULL)"
                )
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT generation FROM catalog_meta WHERE id=1").fetchone()
                generation = int(current[0]) + 1 if current else 1
                connection.execute("DELETE FROM catalog_items")
                connection.executemany(
                    "INSERT INTO catalog_items(relative_path, size, modified_ns, generation) VALUES (?, ?, ?, ?)",
                    [(item.relative_path, item.size, item.modified_ns, generation) for item in current_records],
                )
                connection.execute(
                    "INSERT INTO catalog_meta(id, generation, fingerprint, committed_at) VALUES (1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation, fingerprint=excluded.fingerprint, committed_at=excluded.committed_at",
                    (generation, current_fingerprint, datetime.now(timezone.utc).isoformat()),
                )
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise ProductCatalogError("catalog_integrity_failed")
                final_fingerprint, _final_records, final_quiet = self._scan(product, time.time())
                if not final_quiet or final_fingerprint != current_fingerprint:
                    raise ProductCatalogError("catalog_snapshot_changed")
                for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
                    self._assert_catalog_path(candidate)
                connection.execute("COMMIT")
                return generation
            except ProductCatalogError:
                if connection is not None:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                code = "catalog_database_busy" if "locked" in str(exc).lower() else "catalog_write_failed"
                raise ProductCatalogError(code) from exc
            except (OSError, sqlite3.Error) as exc:
                if connection is not None:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise ProductCatalogError("catalog_write_failed") from exc
            finally:
                if connection is not None:
                    connection.close()

    def _mark_failure(self, product: str, code: str, now: float) -> None:
        with self._state_lock:
            state = self._states[product]
            state.retry_count += 1
            rapid = min(60.0, float(2 ** max(0, state.retry_count - 1)))
            state.next_retry_at = now + (rapid if state.retry_count <= self._max_retries else 300.0)
            # Do not leave a catalog permanently failed: after rapid retries it
            # continues a low-frequency recovery probe until media changes or
            # an operator queues a rescan.
            state.status = "retry_wait"
            state.error_code = code

    def _exists(self, path: Path) -> bool:
        return os.path.exists(self._io_path(path))

    def poll_once(self, *, now: float | None = None) -> None:
        observed_at = float(time.time() if now is None else now)
        for product in PRODUCT_DIRECTORIES:
            try:
                with self._product_locks[product]:
                    with self._state_lock:
                        delayed = self._states[product]
                        if delayed.error_code and observed_at < delayed.next_retry_at:
                            delayed.status = "retry_wait"
                            continue
                    fingerprint, records, quiet = self._scan(product, observed_at)
                    with self._state_lock:
                        state = self._states[product]
                        state.last_scan_at = observed_at
                        state.file_count = len(records)
                        if fingerprint != state.fingerprint:
                            state.fingerprint = fingerprint
                            state.stable_polls = 1
                            state.retry_count = 0
                            state.next_retry_at = 0.0
                            state.error_code = ""
                            state.status = "settling"
                            continue
                        state.stable_polls += 1
                        if not quiet or state.stable_polls < self._stable_polls:
                            state.status = "settling"
                            continue
                        if not state.force_rescan and fingerprint == state.synced_fingerprint:
                            state.status = "watching"
                            continue
                        if observed_at < state.next_retry_at:
                            state.status = "retry_wait"
                            continue
                        state.status = "syncing"
                    try:
                        generation = self._write_generation(product, fingerprint, records)
                    except ProductCatalogError as exc:
                        if str(exc) == "catalog_snapshot_changed":
                            with self._state_lock:
                                state = self._states[product]
                                state.stable_polls = 0
                                state.status = "settling"
                                state.error_code = ""
                            continue
                        self._mark_failure(product, str(exc), observed_at)
                        continue
                    with self._state_lock:
                        state = self._states[product]
                        state.synced_fingerprint = fingerprint
                        state.last_sync_at = observed_at
                        state.generation = generation
                        state.last_good_generation = generation
                        state.retry_count = 0
                        state.next_retry_at = 0.0
                        state.error_code = ""
                        state.force_rescan = False
                        state.status = "watching"
            except ProductCatalogError as exc:
                self._mark_failure(product, str(exc), observed_at)
            except Exception:
                # Never let one operator-writable folder terminate the worker
                # or leave an observable product in the transient syncing state.
                self._mark_failure(product, "catalog_worker_failed", observed_at)

    def reconcile_once(self) -> None:
        """Load the last verified DB generation, then begin normal settlement."""
        self._load_existing_catalogs()
        self.poll_once()

    def _load_existing_catalogs(self) -> None:
        """Fast boot path: validate catalog metadata without walking media folders."""
        observed_at = time.time()
        for product in PRODUCT_DIRECTORIES:
            try:
                with self._product_locks[product]:
                    generation, fingerprint = self._load_existing_catalog(product)
                    if generation:
                        with self._state_lock:
                            state = self._states[product]
                            state.generation = generation
                            state.last_good_generation = generation
                            state.synced_fingerprint = fingerprint
            except ProductCatalogError as exc:
                self._mark_failure(product, str(exc), observed_at)
            except Exception:
                self._mark_failure(product, "catalog_boot_check_failed", observed_at)

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
        # Do not recurse through operator media during ASGI/TestClient startup.
        # Existing committed catalogs remain available while the daemon waits
        # for its normal interval (or an explicit rescan wake-up).
        self._load_existing_catalogs()
        with self._state_lock:
            self._thread = threading.Thread(target=self._run, name="product-media-catalog", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(self._poll_interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception:
                # ``poll_once`` is already per-product defensive; retain this
                # final guard for unexpected programming/runtime failures.
                pass

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))

    def request_rescan(self, product: str | None = None) -> int:
        requested = str(product or "").strip().lower()
        if requested and requested not in PRODUCT_DIRECTORIES:
            raise ProductCatalogError("unknown_product")
        selected = [requested] if requested else list(PRODUCT_DIRECTORIES)
        with self._state_lock:
            for name in selected:
                state = self._states[name]
                state.force_rescan = True
                state.synced_fingerprint = ""
                state.stable_polls = 0
                state.retry_count = 0
                state.next_retry_at = 0.0
                state.error_code = ""
                state.status = "queued"
        self._wake_event.set()
        return len(selected)

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            products = []
            for product, relative in PRODUCT_DIRECTORIES.items():
                state = self._states[product]
                products.append(
                    {
                        "product": product,
                        "directory": relative.replace("/", "\\"),
                        "database": f"Databases/{product}.sqlite3",
                        "state": state.status,
                        "file_count": state.file_count,
                        "generation": state.generation,
                        "last_good_generation": state.last_good_generation,
                        "last_scan_at": _iso(state.last_scan_at),
                        "last_sync_at": _iso(state.last_sync_at),
                        "retry_count": state.retry_count,
                        "error_code": state.error_code,
                    }
                )
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "poll_interval_seconds": self._poll_interval,
                "stable_polls_required": self._stable_polls,
                "minimum_quiet_seconds": self._quiet_seconds,
                "products": products,
            }


_product_media_catalog_service: ProductMediaCatalogService | None = None


def get_product_media_catalog_service() -> ProductMediaCatalogService:
    global _product_media_catalog_service
    if _product_media_catalog_service is None:
        _product_media_catalog_service = ProductMediaCatalogService()
    return _product_media_catalog_service
