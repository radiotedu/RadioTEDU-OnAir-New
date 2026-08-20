from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.run_radio_backend_service import configure_environment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)
    with sqlite3.connect(target) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("database backup integrity check failed")
    return _sha256(target)


def _snapshot_legacy_outputs(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT station_id, local_output_enabled, output_device_id, icecast_enabled, "
            "icecast_host, icecast_port, icecast_mount, icecast_user, icecast_password, "
            "output_gain_db, stream_codec_profile, stream_bitrate_kbps, source_protocol "
            "FROM station_outputs ORDER BY station_id"
        ).fetchall()
    ]


def commission(
    *,
    backup_root: Path,
    enabled: bool = True,
    variants: tuple[str, ...] = ("low", "flac"),
) -> dict[str, object]:
    environment = configure_environment(REPOSITORY_ROOT)
    database = Path(environment["CLEANROOM_DB_PATH"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"cleanroom-before-quality-{timestamp}.db"
    backup_digest = _sqlite_backup(database, backup)

    from app.db import get_connection, init_db
    from app.repositories.settings_repo import SettingsRepository
    from app.repositories.station_repo import StationRepository
    from app.services.quality_output_bridge import write_quality_bridge
    from app.services.quality_outputs import (
        QUALITY_CHANNELS,
        default_quality_outputs,
        external_settings_key,
        match_music_channels,
        quality_suffixes_for_channel,
        replace_quality_outputs,
        serialized_outputs,
    )

    init_db()
    conn = get_connection()
    try:
        settings_repo = SettingsRepository(conn)
        stations = [dict(item) for item in StationRepository(conn).list_all()]
        matched = match_music_channels(stations)
        # Prefer the authoritative saved base mount over station-name guesses.
        # This keeps /radio variants attached to the Pop station even when a
        # retired station is still named exactly "RadioTEDU".
        station_by_mount = {
            str(row["icecast_mount"] or "").strip().lower(): next(
                (
                    station
                    for station in stations
                    if int(station.get("id") or 0) == int(row["station_id"])
                ),
                None,
            )
            for row in conn.execute(
                "SELECT station_id, icecast_mount FROM station_outputs"
            ).fetchall()
        }
        for channel in QUALITY_CHANNELS:
            by_mount = station_by_mount.get(channel.base_mount.lower())
            if by_mount is not None:
                matched[channel.channel_id] = by_mount
        missing = sorted(
            channel.channel_id
            for channel in QUALITY_CHANNELS
            if not channel.external and channel.channel_id not in matched
        )
        if missing:
            raise RuntimeError("station mapping missing: " + ", ".join(missing))

        primary_outputs_changed = 0
        canonical_station_ids: set[int] = set()
        for channel in QUALITY_CHANNELS:
            if channel.external:
                continue
            station_id = int(matched[channel.channel_id]["id"])
            canonical_station_ids.add(station_id)
            row = conn.execute(
                "SELECT icecast_enabled, icecast_mount, stream_codec_profile, "
                "stream_bitrate_kbps FROM station_outputs WHERE station_id=?",
                (station_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"primary station output missing: {channel.channel_id}")
            if str(row["icecast_mount"] or "").strip() != channel.base_mount:
                raise RuntimeError(
                    f"primary mount mismatch for {channel.channel_id}: "
                    f"expected {channel.base_mount}"
                )
            station_settings = settings_repo.get_station(station_id)
            needs_update = (
                not bool(row["icecast_enabled"])
                or str(row["stream_codec_profile"] or "") != "opus_192"
                or int(row["stream_bitrate_kbps"] or 0) != 192
                or str(station_settings.get("broadcast_autostart_enabled", "")).lower()
                != "true"
            )
            conn.execute(
                "UPDATE station_outputs SET icecast_enabled=1, "
                "stream_codec_profile='opus_192', stream_bitrate_kbps=192 "
                "WHERE station_id=?",
                (station_id,),
            )
            settings_repo.upsert_station(
                station_id,
                {
                    "broadcast_autostart_enabled": "true",
                    "stream_codec_profile": "opus_192",
                    "stream_bitrate_kbps": "192",
                },
            )
            if needs_update:
                primary_outputs_changed += 1

        retired_station_autostarts_disabled = 0
        retired_rows = conn.execute(
            "SELECT station_id, value FROM station_settings "
            "WHERE key='broadcast_autostart_enabled'"
        ).fetchall()
        for row in retired_rows:
            station_id = int(row["station_id"])
            if station_id in canonical_station_ids:
                continue
            if str(row["value"] or "").strip().lower() == "true":
                settings_repo.upsert_station(
                    station_id,
                    {"broadcast_autostart_enabled": "false"},
                )
                retired_station_autostarts_disabled += 1

        # The snapshot is intentionally taken after the explicit primary-output
        # commissioning above. Quality fan-out must not mutate protected source
        # credentials, mounts, devices, gain, or protocol after this point.
        before_legacy = _snapshot_legacy_outputs(conn)

        current = settings_repo.get_system()
        # Remove canonical quality mounts from stale station mappings too.
        # A renamed/retired station must never retain an old source fan-out.
        canonical_quality_mounts = {
            f"{channel.base_mount}-{suffix}"
            for channel in QUALITY_CHANNELS
            for suffix in ("low", "normal", "high", "flac")
        }
        # Origin capacity is evidence maintained by diagnostics. Preserve an
        # existing verified value; commissioning must neither manufacture nor
        # erase it while changing the local output plan.
        updates: dict[str, str] = {}
        for key, raw in current.items():
            if not (
                str(key).startswith("station_")
                and str(key).endswith("_extra_icecast_outputs")
            ):
                continue
            try:
                outputs = json.loads(raw or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            changed = False
            for output in outputs if isinstance(outputs, list) else []:
                if not isinstance(output, dict):
                    continue
                mount = str(
                    output.get("icecast_mount") or output.get("mount") or ""
                ).strip()
                if mount in canonical_quality_mounts and output.get("enabled"):
                    output["enabled"] = False
                    changed = True
            if changed:
                updates[str(key)] = serialized_outputs(outputs)
        selected_variants = {str(item).strip().lower() for item in variants}
        unknown_variants = selected_variants - {"low", "flac"}
        if unknown_variants:
            raise RuntimeError(
                "unsupported quality variants: " + ", ".join(sorted(unknown_variants))
            )
        variant_state = {
            suffix: {
                "enabled": bool(enabled and suffix in selected_variants),
                "icecast_public": True,
            }
            for suffix in ("low", "flac")
        }
        for channel in QUALITY_CHANNELS:
            channel_variants = {
                suffix: variant_state[suffix]
                for suffix in quality_suffixes_for_channel(channel)
            }
            if channel.external:
                key = external_settings_key(channel.channel_id)
                updates[key] = serialized_outputs(
                    replace_quality_outputs(channel, [], channel_variants)
                )
                continue
            station_id = int(matched[channel.channel_id]["id"])
            key = f"station_{station_id}_extra_icecast_outputs"
            try:
                existing = json.loads(current.get(key, "") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                existing = []
            updates[key] = serialized_outputs(
                replace_quality_outputs(channel, existing, channel_variants)
            )
        settings_repo.upsert_system(updates)
        readback = settings_repo.get_system()
        if any(readback.get(key) != value for key, value in updates.items()):
            raise RuntimeError("quality settings read-back mismatch")
        bridge = write_quality_bridge(readback)
        if not bridge.get("ok") or int(bridge.get("mount_count") or 0) != 0:
            raise RuntimeError("AI legacy-only bridge verification failed")
        after_legacy = _snapshot_legacy_outputs(conn)
        if before_legacy != after_legacy:
            raise RuntimeError("legacy station output configuration changed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "ok": True,
        "channels": 6,
        "quality_mounts": sum(
            1
            for channel in QUALITY_CHANNELS
            for suffix in quality_suffixes_for_channel(channel)
            if enabled and suffix in selected_variants
        ),
        "quality_outputs_enabled": bool(enabled),
        "primary_outputs_changed": primary_outputs_changed,
        "station_autostart_enabled": 6,
        "retired_station_autostarts_disabled": retired_station_autostarts_disabled,
        "legacy_mounts_changed": False,
        "credentials_persisted_in_quality_settings": False,
        "backup": str(backup),
        "backup_sha256": backup_digest,
        "bridge_backup_verified": bool(bridge.get("backup_verified")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently commission all RadioTEDU quality outputs"
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path(r"H:\RadioTEDU-OnAir-System-Backup\database\quality-commission"),
    )
    parser.add_argument(
        "--disabled",
        action="store_true",
        help="Preserve all canonical settings but disable the 8 additional outputs",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=("low", "flac"),
        default=("low", "flac"),
        help="Enable only these suffixes; omitted suffixes are kept disabled",
    )
    args = parser.parse_args(argv)
    result = commission(
        backup_root=args.backup_root.expanduser().resolve(),
        enabled=not args.disabled,
        variants=tuple(args.variants),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
