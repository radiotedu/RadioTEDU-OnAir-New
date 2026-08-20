from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import zipfile

from app.config import get_data_root
from app.db import get_connection, init_db
from app.security.credential_vault import get_credential_vault
from app.version import PRODUCT_VERSION


_MAX_TEXT_FILE_BYTES = 128 * 1024
_MAX_TEXT_FILES = 40
_MAX_CRASH_RECORDS = 25
_MAX_BUNDLES = 10
_RETENTION_DAYS = 14
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"),
    re.compile(
        r'(?i)(["\']?(?:password|passwd|passphrase|secret|token|api[_-]?key)'
        r'["\']?\s*[:=]\s*)["\']?[^\s,;"\']+'
    ),
    re.compile(r"(?i)(https?://)[^/@\s]+@"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _known_secrets() -> tuple[str, ...]:
    try:
        return tuple(
            str(value)
            for value in get_credential_vault().export_secrets().values()
            if str(value)
        )
    except Exception:
        return ()


def redact_diagnostic_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + "<redacted>", text)
    for secret in sorted(set(item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


def _tail(path: Path, maximum: int = _MAX_TEXT_FILE_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > maximum:
                handle.seek(size - maximum)
            raw = handle.read(maximum)
        if size > maximum:
            raw = raw.split(b"\n", 1)[-1]
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _database_evidence(conn: sqlite3.Connection) -> dict:
    quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    version_row = conn.execute("PRAGMA user_version").fetchone()
    table_names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    counts = {}
    for table in ("stations", "station_outputs", "tracks", "queue", "users"):
        if table not in table_names:
            continue
        counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    return {
        "foreign_key_violation_count": len(foreign_keys),
        "quick_check": quick_check[:10],
        "schema_version": int(version_row[0] if version_row else 0),
        "table_counts": counts,
    }


def _recent_text_files(root: Path) -> list[Path]:
    candidates = []
    for folder in (root / "Logs", root / "State" / "Supervisor", root / "State" / "StationWorkers"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            try:
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in {".json", ".jsonl", ".log", ".txt"}
                ):
                    candidates.append(path)
            except OSError:
                continue
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[:_MAX_TEXT_FILES]


def _crash_inventory(root: Path) -> list[dict]:
    crash_root = root / "CrashDumps"
    if not crash_root.is_dir():
        return []
    candidates = []
    for path in crash_root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                candidates.append(path)
        except OSError:
            continue
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    output = []
    for path in candidates[:_MAX_CRASH_RECORDS]:
        try:
            output.append(
                {
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                    "name": path.name,
                    "size_bytes": int(path.stat().st_size),
                }
            )
        except OSError:
            continue
    return output


def _enforce_retention(root: Path, keep: Path) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_DAYS * 86400
    bundles = sorted(
        root.glob("radiotedu-diagnostics-*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for index, path in enumerate(bundles):
        if path == keep:
            continue
        try:
            if index >= _MAX_BUNDLES or path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def create_radio_diagnostic_bundle(*, health: dict | None = None) -> dict:
    init_db()
    data_root = get_data_root().resolve()
    output_root = (data_root / "Diagnostics").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"radiotedu-diagnostics-{stamp}.zip"
    output = output_root / name
    temporary = output.with_suffix(".tmp")
    secrets = _known_secrets()
    conn = get_connection()
    try:
        database = _database_evidence(conn)
    finally:
        conn.close()
    documents = {
        "manifest.json": json.dumps(
            {
                "created_at": _utc_now(),
                "product": "RadioTEDU OnAir",
                "schema": 1,
                "secret_redaction": "enforced",
                "version": PRODUCT_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "database-evidence.json": json.dumps(database, ensure_ascii=False, sort_keys=True),
        "health.json": json.dumps(dict(health or {}), ensure_ascii=False, sort_keys=True),
        "crash-inventory.json": json.dumps(
            _crash_inventory(data_root),
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    for path in _recent_text_files(data_root):
        try:
            relative = path.resolve().relative_to(data_root)
        except (OSError, ValueError):
            continue
        archive_name = "evidence/" + "/".join(relative.parts)
        documents[archive_name] = _tail(path)

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for archive_name, raw in sorted(documents.items()):
                safe = redact_diagnostic_text(raw, secrets)
                archive.writestr(archive_name, safe.encode("utf-8"))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    _enforce_retention(output_root, output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "name": name,
        "sha256": digest,
        "size_bytes": int(output.stat().st_size),
    }


def list_radio_diagnostic_bundles() -> list[dict]:
    root = (get_data_root() / "Diagnostics").resolve()
    if not root.is_dir():
        return []
    output = []
    for path in sorted(root.glob("radiotedu-diagnostics-*.zip"), reverse=True)[:_MAX_BUNDLES]:
        try:
            output.append(
                {
                    "created_at": datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                    "name": path.name,
                    "size_bytes": int(path.stat().st_size),
                }
            )
        except OSError:
            continue
    return output


def resolve_radio_diagnostic_bundle(name: str) -> Path:
    value = str(name or "").strip()
    if (
        Path(value).name != value
        or not value.startswith("radiotedu-diagnostics-")
        or not value.endswith(".zip")
    ):
        raise ValueError("diagnostic bundle name is invalid")
    root = (get_data_root() / "Diagnostics").resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if not path.is_file() or path.is_symlink():
        raise ValueError("diagnostic bundle was not found")
    return path
