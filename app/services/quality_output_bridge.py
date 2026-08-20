from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_data_root
from app.services.quality_outputs import (
    QUALITY_CHANNELS,
    coerce_bool,
    default_quality_outputs,
    external_settings_key,
    parse_outputs,
)


BRIDGE_SCHEMA_VERSION = 1


def quality_bridge_path() -> Path:
    configured = os.getenv("CLEANROOM_QUALITY_OUTPUT_BRIDGE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (get_data_root() / "integrations" / "quality-outputs.json").resolve()


def quality_bridge_backup_root() -> Path:
    configured = os.getenv(
        "CLEANROOM_QUALITY_OUTPUT_BRIDGE_BACKUP_ROOT", ""
    ).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    durable_h = Path(
        r"H:\RadioTEDU-OnAir-System-Backup\config\quality-outputs"
    )
    if durable_h.drive and Path(f"{durable_h.drive}\\").exists():
        return durable_h
    return (get_data_root() / "backups" / "quality-outputs").resolve()


def build_quality_bridge_payload(settings: dict[str, str]) -> dict:
    channels = []
    for channel in QUALITY_CHANNELS:
        if not channel.external:
            continue
        outputs = parse_outputs(settings.get(external_settings_key(channel.channel_id), ""))
        if not outputs:
            outputs = default_quality_outputs(channel)
        safe_outputs = []
        for output in outputs:
            if not output.get("quality"):
                continue
            safe_outputs.append(
                {
                    "enabled": coerce_bool(output.get("enabled"), True),
                    "quality": str(output["quality"]),
                    "mount": str(
                        output.get("icecast_mount") or output.get("mount") or ""
                    ),
                    "stream_codec_profile": str(
                        output.get("stream_codec_profile") or ""
                    ),
                    "stream_bitrate_kbps": int(
                        output.get("stream_bitrate_kbps") or 0
                    ),
                    "icecast_public": coerce_bool(
                        output.get("icecast_public"), True
                    ),
                    "metadata_suppressed": True,
                    "credential_mode": "inherit_legacy_output",
                }
            )
        channels.append(
            {
                "channel_id": channel.channel_id,
                "base_mount": channel.base_mount,
                "metadata_suppressed": True,
                "single_program_timeline": True,
                "outputs": safe_outputs,
            }
        )
    return {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "credentials_included": False,
        "compliance_counting": "one_play_with_delivered_variants",
        "channels": channels,
    }


def _encoded_payload(payload: dict) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_quality_bridge(settings: dict[str, str]) -> dict:
    payload = build_quality_bridge_payload(settings)
    encoded = _encoded_payload(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    target = quality_bridge_path()
    backup_root = quality_bridge_backup_root()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_parent(target)
    finally:
        if temporary.exists():
            temporary.unlink()

    readback = target.read_bytes()
    if hashlib.sha256(readback).hexdigest() != digest:
        raise OSError("quality bridge read-back verification failed")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root / f"quality-outputs-{timestamp}-{digest[:12]}.json"
    shutil.copy2(target, backup)
    with backup.open("ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
        raise OSError("quality bridge backup verification failed")
    return {
        "ok": True,
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "sha256": digest,
        "backup_verified": True,
        "channel_count": len(payload["channels"]),
        "mount_count": sum(
            len(channel["outputs"]) for channel in payload["channels"]
        ),
    }


def inspect_quality_bridge() -> dict:
    """Return a path-free, secret-free integrity snapshot for operator diagnostics."""
    target = quality_bridge_path()
    if not target.is_file():
        return {
            "ok": False,
            "error_code": "quality_output_bridge_missing",
            "schema_version": None,
            "channel_count": 0,
            "mount_count": 0,
            "credentials_included": False,
        }
    try:
        encoded = target.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
        channels = payload.get("channels") if isinstance(payload, dict) else None
        if not isinstance(channels, list):
            raise ValueError("channels must be a list")
        outputs = [
            output
            for channel in channels
            if isinstance(channel, dict)
            for output in (channel.get("outputs") or [])
            if isinstance(output, dict)
        ]
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        forbidden = (
            '"password"',
            '"icecast_password"',
            '"source_password"',
            '"credential_value"',
        )
        contains_credentials = any(token in serialized for token in forbidden)
        expected_channels = {item.channel_id for item in QUALITY_CHANNELS if item.external}
        actual_channels = {
            str(channel.get("channel_id") or "")
            for channel in channels
            if isinstance(channel, dict)
        }
        ok = bool(
            int(payload.get("schema_version") or 0) == BRIDGE_SCHEMA_VERSION
            and actual_channels == expected_channels
            and len(outputs) == 0
            and not contains_credentials
            and payload.get("credentials_included") is False
        )
        return {
            "ok": ok,
            "error_code": "" if ok else "quality_output_bridge_invalid",
            "schema_version": payload.get("schema_version"),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "channel_count": len(channels),
            "mount_count": len(outputs),
            "credentials_included": contains_credentials,
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "ok": False,
            "error_code": "quality_output_bridge_unreadable",
            "schema_version": None,
            "channel_count": 0,
            "mount_count": 0,
            "credentials_included": False,
        }
