from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


CHANNELS = {
    "/radio": ("/radio-low",),
    "/lofi": ("/lofi-low",),
    "/classic": ("/classic-low", "/classic-flac"),
    "/cazz": ("/cazz-low", "/cazz-flac"),
    "/rock": ("/rock-low",),
    "/energize": ("/energize-low",),
}
RETIRED_SUFFIXES = ("-normal", "-high")


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def verify(database: Path) -> dict[str, object]:
    issues: list[str] = []
    channels: list[dict[str, object]] = []
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        system_settings = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM system_settings")
        }
        outputs = {
            str(row["icecast_mount"] or ""): row
            for row in conn.execute(
                "SELECT station_id, icecast_enabled, icecast_mount, "
                "stream_codec_profile, stream_bitrate_kbps FROM station_outputs"
            )
        }

        approved_quality_mounts: set[str] = set()
        enabled_quality_mounts: set[str] = set()
        for base_mount, expected_variants in CHANNELS.items():
            row = outputs.get(base_mount)
            if row is None:
                issues.append(f"missing primary mount: {base_mount}")
                continue
            station_id = int(row["station_id"])
            profile = str(row["stream_codec_profile"] or "")
            bitrate = int(row["stream_bitrate_kbps"] or 0)
            enabled = bool(row["icecast_enabled"])
            if not enabled or profile != "he_aac_192" or bitrate != 192:
                issues.append(f"primary output policy mismatch: {base_mount}")
            autostart_row = conn.execute(
                "SELECT value FROM station_settings WHERE station_id=? "
                "AND key='broadcast_autostart_enabled'",
                (station_id,),
            ).fetchone()
            autostart = bool(
                autostart_row
                and str(autostart_row["value"] or "").strip().lower() == "true"
            )
            if not autostart:
                issues.append(f"autostart disabled: {base_mount}")

            key = f"station_{station_id}_extra_icecast_outputs"
            try:
                saved = json.loads(system_settings.get(key, "[]") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = []
                issues.append(f"invalid quality settings: {base_mount}")
            saved = [item for item in saved if isinstance(item, dict)]
            current_mounts: set[str] = set()
            for item in saved:
                mount = str(item.get("icecast_mount") or item.get("mount") or "")
                if not _truthy(item.get("enabled")):
                    continue
                enabled_quality_mounts.add(mount)
                if mount.startswith(f"{base_mount}-"):
                    current_mounts.add(mount)
                if mount.endswith(RETIRED_SUFFIXES):
                    issues.append(f"retired output enabled: {mount}")
                if mount.endswith("-flac") and mount not in {
                    "/classic-flac",
                    "/cazz-flac",
                }:
                    issues.append(f"non-approved FLAC enabled: {mount}")
            expected = set(expected_variants)
            approved_quality_mounts.update(expected)
            if current_mounts != expected:
                issues.append(f"quality output mismatch: {base_mount}")
            channels.append(
                {
                    "mount": base_mount,
                    "station_id": station_id,
                    "primary_profile": profile,
                    "primary_bitrate_kbps": bitrate,
                    "autostart": autostart,
                    "quality_mounts": sorted(current_mounts),
                }
            )

        unexpected = enabled_quality_mounts - approved_quality_mounts
        if unexpected:
            issues.append("unexpected enabled quality outputs")
        expected_station_ids = {int(item["station_id"]) for item in channels}
        unexpected_autostarts = [
            int(row["station_id"])
            for row in conn.execute(
                "SELECT station_id, value FROM station_settings "
                "WHERE key='broadcast_autostart_enabled'"
            )
            if int(row["station_id"]) not in expected_station_ids
            and str(row["value"] or "").strip().lower() == "true"
        ]
        if unexpected_autostarts:
            issues.append("retired station autostart is enabled")
        hls_enabled = _truthy(system_settings.get("hls_enabled", "false"))
        legacy_hls_enabled = _truthy(
            system_settings.get("rocket_hls_enabled", "false")
        )
        hls_profile = str(
            system_settings.get("hls_codec_profile", "he_aac_192")
        )
        try:
            hls_bitrate = int(
                float(system_settings.get("hls_bitrate_kbps", "192"))
            )
        except (TypeError, ValueError):
            hls_bitrate = 0
        if hls_enabled or legacy_hls_enabled:
            issues.append("HLS must remain disabled until runtime support is installed")
        if hls_profile != "he_aac_192" or hls_bitrate != 192:
            issues.append("planned HLS profile must remain HE-AAC 192 kbps")
        try:
            origin_capacity = int(
                float(system_settings.get("quality_outputs_origin_source_capacity", "0"))
            )
        except (TypeError, ValueError):
            origin_capacity = 0

    return {
        "ok": not issues,
        "database": str(database),
        "local_mount_count": 14,
        "system_mount_count": 16,
        "origin_source_capacity": origin_capacity,
        "enabled_quality_mount_count": len(enabled_quality_mounts),
        "hls": {
            "enabled": hls_enabled,
            "legacy_origin_enabled": legacy_hls_enabled,
            "runtime_available": False,
            "codec_profile": hls_profile,
            "bitrate_kbps": hls_bitrate,
        },
        "channels": channels,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the approved RadioTEDU 16-mount commissioning state"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(r"C:\ProgramData\RadioTEDU\OnAir\cleanroom.db"),
    )
    args = parser.parse_args()
    result = verify(args.database.expanduser().resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
