from __future__ import annotations

import json
import re
from dataclasses import dataclass


QUALITY_SUFFIXES = ("low", "flac")
LEGACY_QUALITY_SUFFIXES = ("low", "normal", "high", "flac")
FLAC_CHANNEL_IDS = frozenset({"classic", "cazz"})
QUALITY_PROFILES = {
    "low": {
        "label": "Low",
        "codec": "Opus",
        "stream_codec_profile": "opus_32",
        "stream_bitrate_kbps": 32,
    },
    "flac": {
        "label": "Lossless",
        "codec": "FLAC",
        "stream_codec_profile": "ogg_flac_lossless",
        "stream_bitrate_kbps": 0,
    },
}


@dataclass(frozen=True, slots=True)
class QualityChannel:
    channel_id: str
    label: str
    base_mount: str
    station_name_tokens: tuple[str, ...] = ()
    external: bool = False


QUALITY_CHANNELS = (
    QualityChannel("classic", "Classical", "/classic", ("classical",)),
    QualityChannel("lofi", "Lo-Fi", "/lofi", ("lo-fi", "lofi")),
    QualityChannel("cazz", "Jazz", "/cazz", ("jazz",)),
    QualityChannel("energize", "Energetic", "/energize", ("energetic", "energize")),
    QualityChannel("radio", "Pop / Radio", "/radio", ("radiotedu",)),
    QualityChannel("rock", "Rock", "/rock", ("rock",)),
)
QUALITY_CHANNEL_BY_ID = {item.channel_id: item for item in QUALITY_CHANNELS}


def coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off", ""}:
        return False
    return bool(default)


def normalize_mount(value: str) -> str:
    mount = str(value or "").strip()
    if not mount:
        raise ValueError("mount is required")
    if any(ch.isspace() for ch in mount):
        raise ValueError("mount must not contain whitespace")
    return mount if mount.startswith("/") else f"/{mount}"


def quality_mount(base_mount: str, suffix: str) -> str:
    normalized_suffix = str(suffix or "").strip().lower()
    if normalized_suffix not in QUALITY_PROFILES:
        raise ValueError("unsupported quality suffix")
    return f"{normalize_mount(base_mount).rstrip('/')}-{normalized_suffix}"


def quality_suffixes_for_channel(channel: QualityChannel) -> tuple[str, ...]:
    if channel.channel_id in FLAC_CHANNEL_IDS:
        return ("low", "flac")
    return ("low",)


def default_quality_outputs(
    channel: QualityChannel,
    *,
    enabled: bool = False,
) -> list[dict]:
    outputs = []
    for suffix in quality_suffixes_for_channel(channel):
        profile = QUALITY_PROFILES[suffix]
        outputs.append(
            {
                "enabled": bool(enabled),
                "quality": suffix,
                "name": f"{channel.label} — {profile['label']}",
                "icecast_mount": quality_mount(channel.base_mount, suffix),
                "stream_codec_profile": profile["stream_codec_profile"],
                "stream_bitrate_kbps": profile["stream_bitrate_kbps"],
                "icecast_public": True,
                "metadata_suppressed": True,
                # Host, port, source username, and source password deliberately
                # are not persisted here. The runtime inherits the protected
                # legacy station output credentials in memory.
                "credential_mode": "inherit_legacy_output",
            }
        )
    return outputs


def parse_outputs(raw: str | None) -> list[dict]:
    try:
        parsed = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _quality_mounts(channel: QualityChannel) -> set[str]:
    # Remove settings from the retired 24-output plan as well as the current
    # canonical mounts. This prevents stale -normal/-high/non-approved FLAC
    # branches from surviving an idempotent save or recommission.
    return {
        f"{normalize_mount(channel.base_mount).rstrip('/')}-{suffix}"
        for suffix in LEGACY_QUALITY_SUFFIXES
    }


def quality_variant_state(
    channel: QualityChannel,
    existing_outputs: list[dict],
) -> dict[str, dict]:
    expected = {
        quality_mount(channel.base_mount, suffix): suffix
        for suffix in quality_suffixes_for_channel(channel)
    }
    state = {
        suffix: {
            "enabled": False,
            "icecast_public": True,
        }
        for suffix in quality_suffixes_for_channel(channel)
    }
    for output in existing_outputs:
        try:
            mount = normalize_mount(
                output.get("icecast_mount") or output.get("mount") or ""
            )
        except ValueError:
            continue
        suffix = expected.get(mount)
        if suffix is None:
            continue
        state[suffix] = {
            "enabled": coerce_bool(output.get("enabled"), True),
            "icecast_public": coerce_bool(output.get("icecast_public"), True),
        }
    return state


def replace_quality_outputs(
    channel: QualityChannel,
    existing_outputs: list[dict],
    variants: dict[str, dict] | None = None,
) -> list[dict]:
    variants = dict(variants or {})
    allowed = set(quality_suffixes_for_channel(channel))
    unknown = set(variants) - allowed
    if unknown:
        raise ValueError(f"unsupported quality variants: {', '.join(sorted(unknown))}")
    canonical_mounts = _quality_mounts(channel)
    preserved = []
    for output in existing_outputs:
        try:
            mount = normalize_mount(
                output.get("icecast_mount") or output.get("mount") or ""
            )
        except ValueError:
            preserved.append(dict(output))
            continue
        if mount not in canonical_mounts:
            preserved.append(dict(output))

    generated = default_quality_outputs(channel)
    for output in generated:
        suffix = str(output["quality"])
        requested = dict(variants.get(suffix) or {})
        output["enabled"] = coerce_bool(requested.get("enabled"), False)
        output["icecast_public"] = coerce_bool(
            requested.get("icecast_public"), True
        )
    return [*preserved, *generated]


def serialized_outputs(outputs: list[dict]) -> str:
    return json.dumps(outputs, ensure_ascii=False, separators=(",", ":"))


def external_settings_key(channel_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", str(channel_id or "").lower()).strip("_")
    if not safe:
        raise ValueError("channel id is required")
    return f"quality_outputs_external_{safe}"


def match_music_channels(stations: list[dict]) -> dict[str, dict]:
    available = [dict(item) for item in stations]
    matched: dict[str, dict] = {}
    used_station_ids: set[int] = set()
    for channel in QUALITY_CHANNELS:
        if channel.external:
            continue
        exact_mount_candidates = []
        for station in available:
            station_id = int(station.get("id") or 0)
            if station_id in used_station_ids:
                continue
            try:
                station_mount = normalize_mount(
                    station.get("_icecast_mount") or station.get("icecast_mount") or ""
                )
            except ValueError:
                station_mount = ""
            if station_mount == channel.base_mount:
                exact_mount_candidates.append(station)
        if exact_mount_candidates:
            selected = sorted(
                exact_mount_candidates,
                key=lambda item: int(item.get("id") or 0),
            )[0]
            station_id = int(selected.get("id") or 0)
            matched[channel.channel_id] = selected
            used_station_ids.add(station_id)
            continue
        candidates = []
        for station in available:
            station_id = int(station.get("id") or 0)
            if station_id in used_station_ids:
                continue
            name = str(station.get("name") or "").strip().lower()
            if any(token in name for token in channel.station_name_tokens):
                candidates.append(station)
        if channel.channel_id == "radio":
            exact_name = [
                item
                for item in candidates
                if str(item.get("name") or "").strip().lower() == "radiotedu"
            ]
            if exact_name:
                candidates = exact_name
        if candidates:
            selected = sorted(candidates, key=lambda item: int(item.get("id") or 0))[0]
            station_id = int(selected.get("id") or 0)
            matched[channel.channel_id] = selected
            used_station_ids.add(station_id)
    return matched


def public_channel_payload(
    channel: QualityChannel,
    *,
    variants: dict[str, dict],
    station_id: int | None,
    credential_configured: bool,
    credential_status: str,
    applied_by: str,
) -> dict:
    return {
        "channel_id": channel.channel_id,
        "label": channel.label,
        "base_mount": channel.base_mount,
        "recommended_quality": "normal",
        "recommended_mount": normalize_mount(channel.base_mount),
        "primary_mount_operator_managed": True,
        "external": bool(channel.external),
        "station_id": station_id,
        "credential_mode": "inherit_legacy_output",
        "credential_configured": bool(credential_configured),
        "credential_status": str(credential_status or "unknown"),
        "applied_by": str(applied_by or ""),
        "variants": [
            {
                "quality": suffix,
                "label": QUALITY_PROFILES[suffix]["label"],
                "mount": quality_mount(channel.base_mount, suffix),
                "codec": QUALITY_PROFILES[suffix]["codec"],
                "bitrate_kbps": QUALITY_PROFILES[suffix]["stream_bitrate_kbps"],
                "enabled": bool(variants[suffix]["enabled"]),
                "icecast_public": bool(variants[suffix]["icecast_public"]),
            }
            for suffix in quality_suffixes_for_channel(channel)
        ],
    }
