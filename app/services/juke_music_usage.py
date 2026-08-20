"""Read and verify the durable Juke Local music-use ledger.

The media agent writes JSON Lines records to backup storage.  Each record
contains an exact canonical payload and a SHA-256 link to the preceding row.
Only non-secret path keys are read from the protected media-agent env file.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SETTINGS_KEY = "radiotedu_service_control_v1"
_SAFE_ENV_KEYS = frozenset(
    {"JUKE_PLAY_LEDGER_PATH", "LOCAL_MUSIC_OVERFLOW_ROOT", "LOCAL_MUSIC_ROOT"}
)
_MAX_LEDGER_BYTES = 512 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024


class JukeLedgerIntegrityError(RuntimeError):
    pass


def _safe_env_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in _SAFE_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value.strip()
    return values


def discover_juke_ledger_path(conn) -> Path | None:
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key=? LIMIT 1", (_SETTINGS_KEY,)
    ).fetchone()
    try:
        settings = json.loads(str(row[0] if row else ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    juke = settings.get("juke_media_agent")
    if not isinstance(juke, dict):
        return None
    config_path = Path(str(juke.get("config_path") or "").strip())
    if not config_path.is_absolute() or not config_path.is_file():
        return None
    safe = _safe_env_values(config_path)
    explicit = Path(str(safe.get("JUKE_PLAY_LEDGER_PATH") or "").strip())
    if str(explicit) and explicit.is_absolute():
        return explicit.resolve(strict=False)
    storage = str(
        safe.get("LOCAL_MUSIC_OVERFLOW_ROOT")
        or safe.get("LOCAL_MUSIC_ROOT")
        or ""
    ).strip()
    if not storage:
        return None
    storage_path = Path(storage)
    if not storage_path.is_absolute() or not storage_path.anchor:
        return None
    return (
        Path(storage_path.anchor)
        / "RadioTEDU-OnAir-System-Backup"
        / "compliance"
        / "juke-local-music-usage.jsonl"
    ).resolve(strict=False)


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _usage_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"juke:{row.get('event_id', '')}",
        "station_id": None,
        "queue_item_id": None,
        "track_id": None,
        "broadcast_at": row.get("broadcast_at", ""),
        "work_title": row.get("work_title", ""),
        "version": row.get("version_name", ""),
        "performer": row.get("performer", ""),
        "composer": row.get("composer", ""),
        "lyricist": row.get("lyricist", ""),
        "phonogram_producer": row.get("phonogram_producer", ""),
        "label": row.get("label", ""),
        "isrc": row.get("isrc", ""),
        "scheduled_duration_seconds": row.get("scheduled_duration_seconds"),
        "played_duration_seconds": row.get("played_duration_seconds"),
        "publication_count": row.get("publication_count", 1),
        "source_path": row.get("source_path", ""),
        "source_reference": row.get("source_reference", ""),
        "rights_reference": row.get("rights_reference", ""),
        "program_name": row.get("program_name", "Juke Local"),
        "presenter": row.get("presenter", "automation"),
        "log_id": row.get("log_id", ""),
        "previous_hash": row.get("previous_hash", ""),
        "entry_hash": row.get("entry_hash", ""),
        "source_system": "juke_local",
        "ledger_verified": True,
    }


def verify_juke_ledger(
    ledger_path: Path,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not ledger_path.is_file():
        return [], {
            "configured": True,
            "available": False,
            "integrity_ok": True,
            "record_count": 0,
            "last_recorded_at": None,
        }
    try:
        size = ledger_path.stat().st_size
    except OSError as exc:
        raise JukeLedgerIntegrityError("juke_ledger_unreadable") from exc
    if size > _MAX_LEDGER_BYTES:
        raise JukeLedgerIntegrityError("juke_ledger_too_large")

    start = _parse_datetime(date_from)
    end = _parse_datetime(date_to)
    safe_limit = max(1, min(int(limit), 10000))
    previous_hash = ""
    event_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    record_count = 0
    last_recorded_at: str | None = None
    try:
        with ledger_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if len(raw_line) > _MAX_LINE_BYTES:
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_line_too_large:{line_number}"
                    )
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_invalid_json:{line_number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_invalid_entry:{line_number}"
                    )
                canonical = row.get("canonical_payload")
                entry_hash = str(row.get("entry_hash") or "")
                event_id = str(row.get("event_id") or "")
                unsigned = {
                    key: value
                    for key, value in row.items()
                    if key not in {"canonical_payload", "entry_hash"}
                }
                if not isinstance(canonical, str):
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_missing_canonical_payload:{line_number}"
                    )
                try:
                    canonical_value = json.loads(canonical)
                except ValueError as exc:
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_invalid_canonical_payload:{line_number}"
                    ) from exc
                if canonical_value != unsigned:
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_payload_mismatch:{line_number}"
                    )
                computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                if (
                    str(row.get("previous_hash") or "") != previous_hash
                    or computed_hash != entry_hash
                    or not event_id
                    or event_id in event_ids
                ):
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_hash_chain_failed:{line_number}"
                    )
                event_ids.add(event_id)
                previous_hash = entry_hash
                record_count += 1
                last_recorded_at = str(row.get("broadcast_at") or "") or None
                played_at = _parse_datetime(row.get("broadcast_at"))
                if played_at is None:
                    raise JukeLedgerIntegrityError(
                        f"juke_ledger_invalid_broadcast_time:{line_number}"
                    )
                if start and played_at < start:
                    continue
                if end and played_at >= end:
                    continue
                selected.append(_usage_entry(row))
    except OSError as exc:
        raise JukeLedgerIntegrityError("juke_ledger_unreadable") from exc

    selected.sort(key=lambda item: (str(item.get("broadcast_at") or ""), str(item.get("log_id") or "")))
    if len(selected) > safe_limit:
        selected = selected[-safe_limit:]
    return selected, {
        "configured": True,
        "available": True,
        "integrity_ok": True,
        "record_count": record_count,
        "last_recorded_at": last_recorded_at,
    }


def list_juke_music_usage(
    conn,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ledger_path = discover_juke_ledger_path(conn)
    if ledger_path is None:
        return [], {
            "configured": False,
            "available": False,
            "integrity_ok": False,
            "record_count": 0,
            "last_recorded_at": None,
        }
    return verify_juke_ledger(
        ledger_path,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
