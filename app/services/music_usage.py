"""Permanent music-use records for repertoire and rights reporting.

Every completed music play is written as an append-only, hash-chained row.  The
row contains a snapshot of the metadata at air time, so later library edits do
not rewrite the historical report.  CSV exports are deterministic and are
stored beneath the protected RadioTEDU data root for backup and monthly close.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from app.runtime_paths import get_data_dir

_log = logging.getLogger(__name__)

USAGE_COLUMNS = (
    "broadcast_at",
    "station_id",
    "track_id",
    "work_title",
    "version",
    "performer",
    "composer",
    "lyricist",
    "phonogram_producer",
    "label",
    "isrc",
    "scheduled_duration_seconds",
    "played_duration_seconds",
    "publication_count",
    "source_path",
    "source_reference",
    "rights_reference",
    "program_name",
    "presenter",
    "delivered_variants_json",
    "log_id",
    "entry_hash",
)

PLAY_COUNT_COLUMNS = (
    "station_id",
    "station_name",
    "mount",
    "mount_status",
    "track_id",
    "work_title",
    "performer",
    "version",
    "composer",
    "lyricist",
    "phonogram_producer",
    "label",
    "isrc",
    "scheduled_duration_seconds",
    "source_path",
    "completed_play_count",
    "event_count",
    "total_played_seconds",
    "first_broadcast_at",
    "last_broadcast_at",
)

# The current MESAM radio usage form uses these four headings.  We also keep
# the richer rights report above because phonogram licensors need identifiers
# such as ISRC and producer/label that are not present in the minimal form.
MESAM_RADIO_COLUMNS = (
    "Eser Adı",
    "İcracı",
    "Eser Süresi",
    "Yayın Adedi",
)

# Human-readable exports written to the operator's Desktop.  The immutable
# SQLite/hash-chain ledger remains the source of truth; these CSVs are a
# convenient, continuously refreshed mirror for reporting and backup.
PLAY_HISTORY_COLUMNS = (
    "broadcast_at_utc",
    "station_id",
    "station_name",
    "stream_mounts",
    "track_id",
    "song_title",
    "artist",
    "version",
    "composer",
    "lyricist",
    "phonogram_producer",
    "label",
    "isrc",
    "scheduled_duration_seconds",
    "played_duration_seconds",
    "play_count",
    "source_path",
    "program_name",
    "presenter",
    "delivered_variants_json",
    "log_id",
    "entry_hash",
)

HASH_PAYLOAD_COLUMNS = (
    "station_id",
    "queue_item_id",
    "track_id",
    "broadcast_at",
    "work_title",
    "version",
    "performer",
    "composer",
    "lyricist",
    "phonogram_producer",
    "label",
    "isrc",
    "scheduled_duration_seconds",
    "played_duration_seconds",
    "publication_count",
    "source_path",
    "source_reference",
    "rights_reference",
    "program_name",
    "presenter",
    "delivered_variants_json",
    "log_id",
)


def _text(value, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_timestamp(value) -> str:
    if value in (None, ""):
        return _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:32]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_seconds(started_at, finished_at, fallback: float) -> float:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0.0, round((end - start).total_seconds(), 3))
    except (TypeError, ValueError):
        return max(0.0, round(float(fallback or 0.0), 3))


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(int(year), int(month), 1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def _csv_safe(value) -> str:
    text = str(value if value is not None else "")
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def get_play_history_root() -> Path:
    """Return the operator-visible Desktop directory for play-history CSVs.

    Windows services can run with a different profile than the interactive
    operator.  ``RADIOTEDU_PLAY_HISTORY_ROOT`` therefore wins when supplied;
    otherwise we use the current user's Desktop, which is the normal OnAir
    desktop deployment.
    """
    configured = str(os.getenv("RADIOTEDU_PLAY_HISTORY_ROOT", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    profile = str(os.getenv("USERPROFILE", "")).strip()
    if profile:
        return (Path(profile) / "Desktop" / "RadioTEDU Play History").resolve()
    return (Path.home() / "Desktop" / "RadioTEDU Play History").resolve()


def _delivered_variants(value) -> list[dict]:
    normalized = []
    seen = set()
    for item in value or []:
        if not isinstance(item, dict):
            continue
        mount = _text(item.get("mount"), 240)
        if not mount:
            continue
        if not mount.startswith("/"):
            mount = f"/{mount}"
        if mount in seen:
            continue
        seen.add(mount)
        normalized.append(
            {
                "mount": mount,
                "quality": _text(item.get("quality"), 40),
                "codec_profile": _text(item.get("codec_profile"), 80),
                "bitrate_kbps": max(0, int(item.get("bitrate_kbps") or 0)),
            }
        )
    return sorted(normalized, key=lambda item: item["mount"])


class MusicUsageService:
    def __init__(self, conn):
        self.conn = conn

    def upsert_track_metadata(self, track_id: int, payload: dict) -> dict:
        values = {
            "version": _text(payload.get("version")),
            "composer": _text(payload.get("composer")),
            "lyricist": _text(payload.get("lyricist")),
            "phonogram_producer": _text(payload.get("phonogram_producer")),
            "label": _text(payload.get("label")),
            "isrc": _text(payload.get("isrc"), 80).upper(),
            "source_reference": _text(payload.get("source_reference")),
            "rights_reference": _text(payload.get("rights_reference")),
            "source_type": _text(payload.get("source_type"), 120),
            "notes": _text(payload.get("notes")),
        }
        self.conn.execute(
            "INSERT INTO track_broadcast_metadata "
            "(track_id, version, composer, lyricist, phonogram_producer, label, isrc, "
            "source_reference, rights_reference, source_type, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(track_id) DO UPDATE SET version=excluded.version, "
            "composer=excluded.composer, lyricist=excluded.lyricist, "
            "phonogram_producer=excluded.phonogram_producer, label=excluded.label, "
            "isrc=excluded.isrc, source_reference=excluded.source_reference, "
            "rights_reference=excluded.rights_reference, source_type=excluded.source_type, "
            "notes=excluded.notes, updated_at=CURRENT_TIMESTAMP",
            (
                int(track_id),
                values["version"],
                values["composer"],
                values["lyricist"],
                values["phonogram_producer"],
                values["label"],
                values["isrc"],
                values["source_reference"],
                values["rights_reference"],
                values["source_type"],
                values["notes"],
            ),
        )
        self.conn.commit()
        return self.get_track_metadata(int(track_id)) or {"track_id": int(track_id), **values}

    def get_track_metadata(self, track_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT track_id, version, composer, lyricist, phonogram_producer, label, isrc, "
            "source_reference, rights_reference, source_type, notes, updated_at "
            "FROM track_broadcast_metadata WHERE track_id=?",
            (int(track_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_completed_play(
        self,
        *,
        station_id: int,
        track_id: int,
        queue_item_id: int | None = None,
        started_at=None,
        finished_at=None,
        program_name: str = "",
        presenter: str = "",
        delivered_variants: list[dict] | None = None,
        log_id: str | None = None,
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT t.id, t.station_id, t.title, t.artist, t.duration, t.track_type, t.file_path, "
            "m.version, m.composer, m.lyricist, m.phonogram_producer, m.label, m.isrc, "
            "m.source_reference, m.rights_reference "
            "FROM tracks t LEFT JOIN track_broadcast_metadata m ON m.track_id=t.id "
            "WHERE t.id=? LIMIT 1",
            (int(track_id),),
        ).fetchone()
        if row is None or str(row["track_type"] or "music").strip().lower() not in {"music", "jingle"}:
            return None
        stable_log_id = _text(log_id or (f"queue:{int(queue_item_id)}" if queue_item_id else f"track:{int(track_id)}:{_sqlite_timestamp(finished_at)}"), 160)
        existing = self.conn.execute(
            "SELECT * FROM music_usage_log WHERE log_id=? LIMIT 1", (stable_log_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        broadcast_at = _sqlite_timestamp(finished_at)
        scheduled = max(0.0, round(float(row["duration"] or 0.0), 3))
        played = _elapsed_seconds(started_at, finished_at, scheduled)
        metadata = {
            "version": _text(row["version"]),
            "composer": _text(row["composer"]),
            "lyricist": _text(row["lyricist"]),
            "phonogram_producer": _text(row["phonogram_producer"]),
            "label": _text(row["label"]),
            "isrc": _text(row["isrc"], 80).upper(),
            "source_reference": _text(row["source_reference"]),
            "rights_reference": _text(row["rights_reference"]),
        }
        normalized_variants = _delivered_variants(delivered_variants)
        payload = {
            "station_id": int(station_id),
            "queue_item_id": int(queue_item_id) if queue_item_id is not None else None,
            "track_id": int(track_id),
            "broadcast_at": broadcast_at,
            "work_title": _text(row["title"]),
            "version": metadata["version"],
            "performer": _text(row["artist"]),
            "composer": metadata["composer"],
            "lyricist": metadata["lyricist"],
            "phonogram_producer": metadata["phonogram_producer"],
            "label": metadata["label"],
            "isrc": metadata["isrc"],
            "scheduled_duration_seconds": scheduled,
            "played_duration_seconds": played,
            "publication_count": 1,
            "source_path": _text(row["file_path"], 4000),
            "source_reference": metadata["source_reference"],
            "rights_reference": metadata["rights_reference"],
            "program_name": _text(program_name),
            "presenter": _text(presenter),
            "delivered_variants_json": json.dumps(
                normalized_variants,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "log_id": stable_log_id,
        }
        previous = self.conn.execute(
            "SELECT entry_hash FROM music_usage_log WHERE station_id=? ORDER BY id DESC LIMIT 1",
            (int(station_id),),
        ).fetchone()
        previous_hash = _text(previous[0] if previous else "", 128)
        canonical = json.dumps({**payload, "previous_hash": previous_hash}, sort_keys=True, separators=(",", ":"))
        entry_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            self.conn.execute(
                "INSERT INTO music_usage_log "
                "(station_id, queue_item_id, track_id, broadcast_at, work_title, version, performer, composer, lyricist, "
                "phonogram_producer, label, isrc, scheduled_duration_seconds, played_duration_seconds, publication_count, "
                "source_path, source_reference, rights_reference, program_name, presenter, delivered_variants_json, "
                "log_id, metadata_snapshot_json, previous_hash, entry_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["station_id"], payload["queue_item_id"], payload["track_id"], payload["broadcast_at"],
                    payload["work_title"], payload["version"], payload["performer"], payload["composer"], payload["lyricist"],
                    payload["phonogram_producer"], payload["label"], payload["isrc"], payload["scheduled_duration_seconds"],
                    payload["played_duration_seconds"], payload["publication_count"], payload["source_path"],
                    payload["source_reference"], payload["rights_reference"], payload["program_name"], payload["presenter"],
                    payload["delivered_variants_json"], payload["log_id"], json.dumps(
                        {**metadata, "delivered_variants": normalized_variants},
                        ensure_ascii=False,
                        sort_keys=True,
                    ), previous_hash, entry_hash,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            existing = self.conn.execute(
                "SELECT * FROM music_usage_log WHERE log_id=? LIMIT 1", (stable_log_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            raise
        recorded = dict(
            self.conn.execute(
                "SELECT * FROM music_usage_log WHERE log_id=?", (stable_log_id,)
            ).fetchone()
        )
        # CSV mirrors are refreshed asynchronously so the audio worker never
        # waits on a large all-time export.  The immutable row above is already
        # durable before the notification is sent.
        request_music_usage_export()
        return recorded

    def list_entries(self, *, station_id: int | None = None, date_from: str | None = None, date_to: str | None = None, limit: int | None = 1000) -> list[dict]:
        where: list[str] = []
        params: list = []
        if station_id is not None:
            where.append("station_id=?")
            params.append(int(station_id))
        if date_from:
            where.append("broadcast_at>=?")
            params.append(_sqlite_timestamp(date_from))
        if date_to:
            where.append("broadcast_at<?")
            params.append(_sqlite_timestamp(date_to))
        sql = "SELECT * FROM music_usage_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY broadcast_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]

    def list_play_counts(
        self,
        *,
        station_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        music_only: bool = False,
    ) -> list[dict]:
        where: list[str] = []
        params: list = []
        if music_only:
            # Historical rows survive library cleanup.  Missing track rows are
            # therefore treated as music, while explicitly typed jingles,
            # promos, advertisements and speech stay out of licensor totals.
            where.append("COALESCE(t.track_type, 'music')='music'")
        if station_id is not None:
            where.append("l.station_id=?")
            params.append(int(station_id))
        if date_from:
            where.append("l.broadcast_at>=?")
            params.append(_sqlite_timestamp(date_from))
        if date_to:
            where.append("l.broadcast_at<?")
            params.append(_sqlite_timestamp(date_to))
        sql = (
            "SELECT l.station_id, COALESCE(s.name, '') AS station_name, "
            "COALESCE(o.icecast_mount, '') AS mount, "
            "CASE WHEN COALESCE(o.icecast_enabled, 0)=1 "
            "AND TRIM(COALESCE(o.icecast_mount, ''))<>'' "
            "THEN 'configured_stream' ELSE 'historical_or_disabled' END AS mount_status, "
            "l.track_id, l.work_title, l.performer, l.version, l.composer, l.lyricist, "
            "l.phonogram_producer, l.label, l.isrc, "
            "ROUND(MAX(COALESCE(l.scheduled_duration_seconds, 0)), 3) "
            "AS scheduled_duration_seconds, l.source_path, "
            "SUM(COALESCE(l.publication_count, 1)) AS completed_play_count, "
            "COUNT(*) AS event_count, "
            "ROUND(SUM(COALESCE(l.played_duration_seconds, 0)), 3) AS total_played_seconds, "
            "MIN(l.broadcast_at) AS first_broadcast_at, MAX(l.broadcast_at) AS last_broadcast_at "
            "FROM music_usage_log l LEFT JOIN stations s ON s.id=l.station_id "
            "LEFT JOIN station_outputs o ON o.station_id=l.station_id "
            "LEFT JOIN tracks t ON t.id=l.track_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " GROUP BY l.station_id, s.name, o.icecast_mount, o.icecast_enabled, "
            "l.track_id, l.work_title, l.performer, l.version, "
            "l.composer, l.lyricist, l.phonogram_producer, "
            "l.label, l.isrc, l.source_path "
            "ORDER BY l.station_id ASC, completed_play_count DESC, "
            "LOWER(l.work_title) ASC, LOWER(l.performer) ASC, l.track_id ASC"
        )
        return [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]

    def verify_hash_chain(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM music_usage_log ORDER BY station_id ASC, id ASC"
        ).fetchall()
        expected_previous_by_station: dict[int, str] = {}
        final_hashes: dict[str, str] = {}
        hash_versions = {"v1_without_delivered_variants": 0, "v2": 0}
        for row in rows:
            entry = dict(row)
            station_id = int(entry.get("station_id") or 0)
            expected_previous = expected_previous_by_station.get(station_id, "")
            payload = {column: entry.get(column) for column in HASH_PAYLOAD_COLUMNS}
            canonical = json.dumps(
                {**payload, "previous_hash": expected_previous},
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if _text(entry.get("previous_hash"), 128) != expected_previous:
                return {
                    "valid": False,
                    "record_count": len(rows),
                    "station_id": station_id,
                    "log_id": _text(entry.get("log_id"), 160),
                    "reason": "previous_hash_mismatch",
                }
            stored_hash = _text(entry.get("entry_hash"), 128)
            hash_version = "v2"
            if stored_hash != expected_hash:
                # Version 1 predates delivery-variant reporting. Schema v20
                # added the column with a default without rewriting immutable
                # historical rows, so the original canonical payload remains
                # a valid and necessary verification path.
                legacy_payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "delivered_variants_json"
                }
                legacy_canonical = json.dumps(
                    {**legacy_payload, "previous_hash": expected_previous},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                expected_hash = hashlib.sha256(
                    legacy_canonical.encode("utf-8")
                ).hexdigest()
                hash_version = "v1_without_delivered_variants"
            if stored_hash != expected_hash:
                return {
                    "valid": False,
                    "record_count": len(rows),
                    "station_id": station_id,
                    "log_id": _text(entry.get("log_id"), 160),
                    "reason": "entry_hash_mismatch",
                }
            expected_previous_by_station[station_id] = expected_hash
            final_hashes[str(station_id)] = expected_hash
            hash_versions[hash_version] += 1
        return {
            "valid": True,
            "record_count": len(rows),
            "station_final_hashes": final_hashes,
            "hash_versions": hash_versions,
        }

    def csv_text(self, entries: list[dict]) -> str:
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(USAGE_COLUMNS)
        for entry in entries:
            writer.writerow([_csv_safe(entry.get(column, "")) for column in USAGE_COLUMNS])
        return output.getvalue()

    def play_count_csv_text(self, entries: list[dict]) -> str:
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(PLAY_COUNT_COLUMNS)
        for entry in entries:
            writer.writerow(
                [_csv_safe(entry.get(column, "")) for column in PLAY_COUNT_COLUMNS]
            )
        return output.getvalue()

    def mesam_radio_csv_text(self, entries: list[dict]) -> str:
        """Render one station's completed plays in MESAM's radio-form shape."""

        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(MESAM_RADIO_COLUMNS)
        for entry in entries:
            duration = max(0, int(round(float(entry.get("scheduled_duration_seconds") or 0))))
            hours, remainder = divmod(duration, 3600)
            minutes, seconds = divmod(remainder, 60)
            writer.writerow(
                (
                    _csv_safe(entry.get("work_title", "")),
                    _csv_safe(entry.get("performer", "")),
                    f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                    _csv_safe(entry.get("completed_play_count", 0)),
                )
            )
        # A BOM makes the Turkish headings deterministic in desktop Excel.
        return "\ufeff" + output.getvalue()

    def _export_mesam_station_forms(
        self,
        *,
        destination: str | Path,
        label: str,
        entries: list[dict],
    ) -> list[dict]:
        grouped: dict[int, list[dict]] = {}
        for entry in entries:
            station_id = int(entry.get("station_id") or 0)
            grouped.setdefault(station_id, []).append(entry)
        exports: list[dict] = []
        for station_id in sorted(grouped):
            station_entries = grouped[station_id]
            result = self._atomic_write_text(
                Path(destination) / f"{label}-station-{station_id}-radio-form.csv",
                self.mesam_radio_csv_text(station_entries),
            )
            exports.append(
                {
                    **result,
                    "station_id": station_id,
                    "station_name": str(station_entries[0].get("station_name") or ""),
                    "record_count": len(station_entries),
                }
            )
        return exports

    def _station_context_entries(self, entries: list[dict]) -> list[dict]:
        """Add station names and every delivered mount to user-facing rows."""
        if not entries:
            return []
        station_ids = sorted(
            {
                int(entry.get("station_id"))
                for entry in entries
                if str(entry.get("station_id") or "").strip().isdigit()
            }
        )
        contexts: dict[int, dict[str, str]] = {}
        if station_ids:
            placeholders = ",".join("?" for _ in station_ids)
            try:
                rows = self.conn.execute(
                    "SELECT s.id, COALESCE(s.name, '') AS station_name, "
                    "COALESCE(o.icecast_mount, '') AS mount "
                    "FROM stations s LEFT JOIN station_outputs o "
                    f"ON o.station_id=s.id WHERE s.id IN ({placeholders})",
                    tuple(station_ids),
                ).fetchall()
                contexts = {
                    int(row["id"]): {
                        "station_name": str(row["station_name"] or ""),
                        "mount": str(row["mount"] or ""),
                    }
                    for row in rows
                }
            except Exception:
                # Reporting must remain available on legacy databases that do
                # not yet have the station-output table.
                contexts = {}

        enriched: list[dict] = []
        for entry in entries:
            item = dict(entry)
            try:
                station_id = int(item.get("station_id") or 0)
            except (TypeError, ValueError):
                station_id = 0
            context = contexts.get(station_id, {})
            mounts: set[str] = set()
            primary = str(context.get("mount") or "").strip()
            if primary:
                mounts.add(primary if primary.startswith("/") else f"/{primary}")
            try:
                variants = json.loads(str(item.get("delivered_variants_json") or "[]"))
            except (TypeError, ValueError):
                variants = []
            for variant in variants if isinstance(variants, list) else []:
                if not isinstance(variant, dict):
                    continue
                mount = str(variant.get("mount") or "").strip()
                if mount:
                    mounts.add(mount if mount.startswith("/") else f"/{mount}")
            item["station_name"] = str(context.get("station_name") or f"Station {station_id}")
            item["stream_mounts"] = " ".join(sorted(mounts))
            enriched.append(item)
        return enriched

    def play_history_csv_text(self, entries: list[dict]) -> str:
        """Render a compact, human-readable all-radio play-history CSV."""
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(PLAY_HISTORY_COLUMNS)
        for entry in self._station_context_entries(entries):
            values = {
                "broadcast_at_utc": entry.get("broadcast_at", ""),
                "station_id": entry.get("station_id", ""),
                "station_name": entry.get("station_name", ""),
                "stream_mounts": entry.get("stream_mounts", ""),
                "track_id": entry.get("track_id", ""),
                "song_title": entry.get("work_title", ""),
                "artist": entry.get("performer", ""),
                "version": entry.get("version", ""),
                "composer": entry.get("composer", ""),
                "lyricist": entry.get("lyricist", ""),
                "phonogram_producer": entry.get("phonogram_producer", ""),
                "label": entry.get("label", ""),
                "isrc": entry.get("isrc", ""),
                "scheduled_duration_seconds": entry.get("scheduled_duration_seconds", ""),
                "played_duration_seconds": entry.get("played_duration_seconds", ""),
                "play_count": entry.get("publication_count", 1),
                "source_path": entry.get("source_path", ""),
                "program_name": entry.get("program_name", ""),
                "presenter": entry.get("presenter", ""),
                "delivered_variants_json": entry.get("delivered_variants_json", "[]"),
                "log_id": entry.get("log_id", ""),
                "entry_hash": entry.get("entry_hash", ""),
            }
            writer.writerow([_csv_safe(values[column]) for column in PLAY_HISTORY_COLUMNS])
        return output.getvalue()

    def export_desktop_bundle(
        self,
        *,
        destination: str | Path | None = None,
        now: date | None = None,
    ) -> dict:
        """Refresh daily and all-time CSVs in one Desktop folder.

        Existing protected exports are copied into ``legacy`` once, so a
        previous installation's reports remain available alongside the new
        all-radio mirror instead of being silently replaced.
        """
        root = Path(destination or get_play_history_root()).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        daily_root = root / "daily"
        licensor_root = root / "licensor"
        mesam_root = licensor_root / "MESAM"
        legacy_root = root / "legacy"
        daily_root.mkdir(parents=True, exist_ok=True)
        licensor_root.mkdir(parents=True, exist_ok=True)
        mesam_root.mkdir(parents=True, exist_ok=True)
        legacy_root.mkdir(parents=True, exist_ok=True)

        previous_exports = get_data_dir() / "Exports" / "MusicUsage"
        if previous_exports.is_dir():
            for source in previous_exports.glob("*.csv"):
                target = legacy_root / source.name
                if not target.exists():
                    try:
                        shutil.copy2(source, target)
                    except OSError:
                        _log.warning("Could not preserve legacy music-use export %s", source)

        current_day = now or _utc_now().date()
        day_values = [current_day]
        previous_day = current_day - timedelta(days=1)
        if previous_day != current_day:
            day_values.append(previous_day)
        daily_results: list[dict] = []
        for day in day_values:
            next_day = day + timedelta(days=1)
            entries = self.list_entries(
                date_from=day.isoformat(),
                date_to=next_day.isoformat(),
                limit=None,
            )
            label = day.isoformat()
            event_export = self._atomic_write_text(
                daily_root / f"{label}-all-radios.csv",
                self.play_history_csv_text(entries),
            )
            counts = self.list_play_counts(
                date_from=day.isoformat(), date_to=next_day.isoformat()
            )
            count_export = self._atomic_write_text(
                daily_root / f"{label}-play-counts.csv",
                self.play_count_csv_text(counts),
            )
            rights_counts = self.list_play_counts(
                date_from=day.isoformat(),
                date_to=next_day.isoformat(),
                music_only=True,
            )
            rights_export = self._atomic_write_text(
                licensor_root / f"{label}-all-radios-rights-report.csv",
                self.play_count_csv_text(rights_counts),
            )
            mesam_exports = self._export_mesam_station_forms(
                destination=mesam_root,
                label=label,
                entries=rights_counts,
            )
            daily_results.append(
                {
                    "date": label,
                    "events": {**event_export, "record_count": len(entries)},
                    "play_counts": {**count_export, "record_count": len(counts)},
                    "rights_report": {
                        **rights_export,
                        "record_count": len(rights_counts),
                    },
                    "mesam_station_forms": mesam_exports,
                }
            )

        all_entries = self.list_entries(limit=None)
        total_events = self._atomic_write_text(
            root / "RadioTEDU-play-history-total.csv",
            self.play_history_csv_text(all_entries),
        )
        all_counts = self.list_play_counts()
        total_counts = self._atomic_write_text(
            root / "RadioTEDU-play-counts-total.csv",
            self.play_count_csv_text(all_counts),
        )
        all_rights_counts = self.list_play_counts(music_only=True)
        total_rights = self._atomic_write_text(
            licensor_root / "RadioTEDU-rights-report-total.csv",
            self.play_count_csv_text(all_rights_counts),
        )
        total_mesam = self._export_mesam_station_forms(
            destination=mesam_root,
            label="all-time",
            entries=all_rights_counts,
        )

        # Stable aliases make it easy for operators and backup jobs to consume
        # the current day without having to calculate a date in a shell.
        today_entries = self.list_entries(
            date_from=current_day.isoformat(),
            date_to=(current_day + timedelta(days=1)).isoformat(),
            limit=None,
        )
        today_counts = self.list_play_counts(
            date_from=current_day.isoformat(),
            date_to=(current_day + timedelta(days=1)).isoformat(),
        )
        daily_alias = self._atomic_write_text(
            root / "RadioTEDU-play-history-daily.csv",
            self.play_history_csv_text(today_entries),
        )
        daily_count_alias = self._atomic_write_text(
            root / "RadioTEDU-play-counts-daily.csv",
            self.play_count_csv_text(today_counts),
        )
        today_rights_counts = self.list_play_counts(
            date_from=current_day.isoformat(),
            date_to=(current_day + timedelta(days=1)).isoformat(),
            music_only=True,
        )
        daily_rights_alias = self._atomic_write_text(
            licensor_root / "RadioTEDU-rights-report-daily.csv",
            self.play_count_csv_text(today_rights_counts),
        )
        manifest = {
            "generated_at_utc": _utc_now().isoformat().replace("+00:00", "Z"),
            "source": "RadioTEDU OnAir immutable music_usage_log",
            "daily": daily_results,
            "daily_alias": {**daily_alias, "record_count": len(today_entries)},
            "daily_count_alias": {**daily_count_alias, "record_count": len(today_counts)},
            "daily_rights_alias": {
                **daily_rights_alias,
                "record_count": len(today_rights_counts),
            },
            "total": {**total_events, "record_count": len(all_entries)},
            "total_play_counts": {**total_counts, "record_count": len(all_counts)},
            "total_rights_report": {
                **total_rights,
                "record_count": len(all_rights_counts),
            },
            "total_mesam_station_forms": total_mesam,
            "integrity": self.verify_hash_chain(),
            "legacy_exports_preserved_at": str(legacy_root),
        }
        manifest_export = self._atomic_write_text(
            root / "RadioTEDU-play-history-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"root": str(root), "manifest": manifest_export, **manifest}

    @staticmethod
    def _atomic_write_text(destination: str | Path, content: str) -> dict:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="")
        temporary.replace(target)
        return {
            "path": str(target),
            "checksum": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    def export_csv(self, *, destination: str | Path, station_id: int | None = None, date_from: str | None = None, date_to: str | None = None, entries: list[dict] | None = None) -> dict:
        if entries is None:
            entries = self.list_entries(
                station_id=station_id,
                date_from=date_from,
                date_to=date_to,
                limit=None,
            )
        result = self._atomic_write_text(destination, self.csv_text(entries))
        return {**result, "record_count": len(entries)}

    def export_official_current(self, *, destination: str | Path) -> dict:
        root = Path(destination)
        entries = self.list_entries(limit=None)
        integrity = self.verify_hash_chain()
        event_export = self.export_csv(
            destination=root / "RadioTEDU-music-usage-events-current.csv",
            entries=entries,
        )
        play_counts = self.list_play_counts()
        count_export = self._atomic_write_text(
            root / "RadioTEDU-music-play-counts-current.csv",
            self.play_count_csv_text(play_counts),
        )
        count_export["record_count"] = len(play_counts)
        rights_counts = self.list_play_counts(music_only=True)
        rights_export = self._atomic_write_text(
            root / "RadioTEDU-rights-report-current.csv",
            self.play_count_csv_text(rights_counts),
        )
        rights_export["record_count"] = len(rights_counts)
        mesam_exports = self._export_mesam_station_forms(
            destination=root / "MESAM",
            label="current",
            entries=rights_counts,
        )
        generated_at = _utc_now().isoformat().replace("+00:00", "Z")
        manifest = {
            "generated_at_utc": generated_at,
            "integrity": integrity,
            "events": event_export,
            "play_counts": count_export,
            "rights_report": rights_export,
            "mesam_station_forms": mesam_exports,
            "definitions": {
                "completed_play_count": "Sum of immutable publication_count values after playout completion.",
                "event_count": "Number of immutable completed-play ledger rows.",
                "timestamps": "UTC",
            },
        }
        manifest_export = self._atomic_write_text(
            root / "RadioTEDU-music-usage-integrity-current.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {
            "generated_at_utc": generated_at,
            "integrity": integrity,
            "events": event_export,
            "play_counts": count_export,
            "rights_report": rights_export,
            "mesam_station_forms": mesam_exports,
            "manifest": manifest_export,
        }

    def close_month(self, *, year: int, month: int, closed_by: str = "system", export_path: str | Path | None = None, extra_entries: list[dict] | None = None) -> dict:
        start, end = _month_bounds(int(year), int(month))
        period_key = f"{int(year):04d}-{int(month):02d}"
        existing = self.conn.execute(
            "SELECT * FROM music_usage_month_closures WHERE period_key=?", (period_key,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        entries = self.list_entries(date_from=start, date_to=end, limit=None)
        if extra_entries:
            entries.extend(extra_entries)
            entries.sort(key=lambda entry: (str(entry.get("broadcast_at") or ""), str(entry.get("log_id") or "")))
        if export_path is None:
            export_path = get_data_dir() / "Exports" / "MusicUsage" / f"{period_key}.csv"
        export = self.export_csv(destination=export_path, entries=entries)
        digest = hashlib.sha256("\n".join(str(entry["entry_hash"]) for entry in entries).encode("utf-8")).hexdigest()
        first_hash = str(entries[0]["entry_hash"]) if entries else ""
        last_hash = str(entries[-1]["entry_hash"]) if entries else ""
        self.conn.execute(
            "INSERT INTO music_usage_month_closures "
            "(period_key, period_start, period_end, record_count, first_entry_hash, last_entry_hash, export_path, checksum, closed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (period_key, start, end, len(entries), first_hash, last_hash, str(export["path"]), digest, _text(closed_by, 160)),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM music_usage_month_closures WHERE period_key=?", (period_key,)).fetchone())

    def ensure_daily_exports(self, *, now: date | None = None) -> dict:
        today = now or _utc_now().date()
        previous = today - timedelta(days=1)
        destination = get_data_dir() / "Exports" / "MusicUsage" / f"{previous.isoformat()}.csv"
        daily = self.export_csv(destination=destination, date_from=previous.isoformat(), date_to=today.isoformat())
        closed = None
        if today.day == 1:
            month = previous.month
            closed = self.close_month(year=previous.year, month=month)
        desktop = self.export_desktop_bundle(now=today)
        return {"daily": daily, "monthly_close": closed, "desktop": desktop}


class MusicUsageExportScheduler:
    """Refresh CSV mirrors without ever blocking a station audio worker."""

    def __init__(self, interval_seconds: float = 300.0, minimum_export_gap: float = 15.0):
        self.interval_seconds = max(30.0, float(interval_seconds))
        self.minimum_export_gap = max(1.0, float(minimum_export_gap))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_export_monotonic = 0.0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(
                target=self._run,
                name="music-usage-export",
                daemon=True,
            )
            self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def run_once(self) -> dict | None:
        """Run one export pass; useful to the standalone backup task/tests."""
        try:
            from app.db import get_connection

            conn = get_connection()
            try:
                return MusicUsageService(conn).ensure_daily_exports()
            finally:
                conn.close()
        except Exception:
            _log.exception("Music-use export pass failed")
            return None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            wait_for = max(0.0, self.minimum_export_gap - (now - self._last_export_monotonic))
            if wait_for > 0.0:
                self._wake.wait(timeout=wait_for)
                self._wake.clear()
                if self._stop.is_set():
                    return
            self._last_export_monotonic = time.monotonic()
            self.run_once()
            self._wake.wait(timeout=self.interval_seconds)
            self._wake.clear()


music_usage_export_scheduler = MusicUsageExportScheduler()


def request_music_usage_export() -> None:
    """Signal the background exporter after an immutable play is recorded."""
    try:
        music_usage_export_scheduler.notify()
    except Exception:
        # Never allow reporting telemetry to affect playout reliability.
        _log.debug("Could not signal music-use exporter", exc_info=True)
