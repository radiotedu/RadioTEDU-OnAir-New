"""One-time migration from the legacy Ogg/Opus station outputs to HE-AAC v1.

The migration is deliberately limited to RadioTEDU's canonical music mounts.
It never changes the two lossless FLAC mounts and it is idempotent, so a
backend restart cannot create duplicate changes. Existing live workers keep
their current encoder until their next output refresh/track; no process is
stopped here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


MIGRATION_KEY = "codec_migration.ogg_to_he_aac_v1"
CANONICAL_MAIN_MOUNTS = frozenset(
    {"/classic", "/lofi", "/radio", "/cazz", "/rock", "/energize"}
)
FLAC_MOUNTS = frozenset({"/classic-flac", "/cazz-flac"})


def _normalize_mount(value: object) -> str:
    mount = str(value or "").strip().lower()
    if not mount:
        return ""
    return mount if mount.startswith("/") else f"/{mount}"


def _is_legacy_ogg_profile(value: object) -> bool:
    return str(value or "").strip().lower().startswith("opus_")


def migrate_ogg_outputs_to_he_aac(conn, logger=None) -> dict[str, int | bool]:
    """Convert canonical Opus/Ogg output rows and low branches in-place."""

    marker_row = conn.execute(
        "SELECT value FROM system_settings WHERE key=?",
        (MIGRATION_KEY,),
    ).fetchone()
    if marker_row and str(marker_row[0] or "").strip():
        return {"already_applied": True, "primary_changed": 0, "extra_changed": 0}

    primary_changed = 0
    extra_changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT station_id, icecast_mount, stream_codec_profile FROM station_outputs"
        ).fetchall()
        for row in rows:
            mount = _normalize_mount(row[1])
            if mount not in CANONICAL_MAIN_MOUNTS or mount in FLAC_MOUNTS:
                continue
            if not _is_legacy_ogg_profile(row[2]):
                continue
            station_id = int(row[0])
            conn.execute(
                "UPDATE station_outputs SET stream_codec_profile=?, stream_bitrate_kbps=? WHERE station_id=?",
                ("he_aac_192", 192, station_id),
            )
            conn.execute(
                "UPDATE station_settings SET value=?, updated_at=CURRENT_TIMESTAMP WHERE station_id=? AND key='stream_codec_profile'",
                ("he_aac_192", station_id),
            )
            conn.execute(
                "UPDATE station_settings SET value=?, updated_at=CURRENT_TIMESTAMP WHERE station_id=? AND key='stream_bitrate_kbps'",
                ("192", station_id),
            )
            primary_changed += 1

        extra_rows = conn.execute(
            "SELECT key, value FROM system_settings WHERE key LIKE 'station_%_extra_icecast_outputs'"
        ).fetchall()
        for row in extra_rows:
            try:
                outputs = json.loads(str(row[1] or ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(outputs, list):
                continue
            changed = False
            for output in outputs:
                if not isinstance(output, dict):
                    continue
                mount = _normalize_mount(output.get("icecast_mount") or output.get("mount"))
                if not mount.endswith("-low") or mount in FLAC_MOUNTS:
                    continue
                if not _is_legacy_ogg_profile(output.get("stream_codec_profile")):
                    continue
                output["stream_codec_profile"] = "he_aac_96"
                output["stream_bitrate_kbps"] = 96
                changed = True
                extra_changed += 1
            if changed:
                encoded = json.dumps(outputs, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    "UPDATE system_settings SET value=?, updated_at=CURRENT_TIMESTAMP WHERE key=?",
                    (encoded, str(row[0])),
                )

        stamp = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (MIGRATION_KEY, stamp),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    summary = {"already_applied": False, "primary_changed": primary_changed, "extra_changed": extra_changed}
    if logger is not None and (primary_changed or extra_changed):
        logger.info("Ogg/Opus outputs migrated to HE-AAC v1: primary=%d extra=%d", primary_changed, extra_changed)
    return summary
