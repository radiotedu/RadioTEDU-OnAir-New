import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_any_permission, require_permission
from app.db import get_connection, init_db
from app.engine.ad_policy import rocket_ad_insertion_enabled_from_settings
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.repositories.station_repo import StationRepository
from app.repositories.log_repo import LogRepository
from app.security.credential_vault import (
    is_credential_reference,
    resolve_credential_value,
    store_system_secret,
)
from app.services.quality_outputs import (
    QUALITY_CHANNELS,
    QUALITY_CHANNEL_BY_ID,
    external_settings_key,
    match_music_channels,
    parse_outputs,
    public_channel_payload,
    quality_variant_state,
    replace_quality_outputs,
    serialized_outputs,
)
from app.services.quality_output_bridge import (
    inspect_quality_bridge,
    write_quality_bridge,
)
from app.services.encoder_capabilities import inspect_opus_encoder

router = APIRouter()

LOCAL_MUSIC_MOUNT_COUNT = 14
EXTERNAL_AI_MOUNT_COUNT = 2
SYSTEM_MOUNT_COUNT = LOCAL_MUSIC_MOUNT_COUNT + EXTERNAL_AI_MOUNT_COUNT
REQUIRED_ORIGIN_SOURCE_SLOTS = SYSTEM_MOUNT_COUNT
RECOMMENDED_ORIGIN_SOURCE_SLOTS = 20


class StreamingFeatureSettingsUpdate(BaseModel):
    stream_public_base_url: str = ""
    radio_website_url: str = ""
    rocket_admin_user: str = "admin"
    rocket_admin_password: str = ""
    rocket_health_password: str = ""
    rocket_status_page_enabled: bool = True
    rocket_hls_enabled: bool = True
    rocket_fallbacks_enabled: bool = True
    rocket_listener_auth_enabled: bool = False
    rocket_ad_insertion_enabled: bool = False
    rocket_access_log_enabled: bool = True
    rocket_playlist_log_enabled: bool = True


class QualityVariantSettingsUpdate(BaseModel):
    enabled: bool = True
    icecast_public: bool = True


class QualityChannelSettingsUpdate(BaseModel):
    channel_id: str
    variants: dict[str, QualityVariantSettingsUpdate]


class QualityOutputsSettingsUpdate(BaseModel):
    channels: list[QualityChannelSettingsUpdate]
    origin_source_capacity: int | None = None


class QualityOutputsApplyRequest(BaseModel):
    restart_ai_supervisor: bool = False


class MetadataUpdatePayload(BaseModel):
    station_id: int = 0
    mount: str = ""
    song: str


class MoveListenersPayload(BaseModel):
    station_id: int = 0
    mount: str
    destination: str


class KickSourcePayload(BaseModel):
    station_id: int = 0
    mount: str


class MidrollPayload(BaseModel):
    station_id: int = 0
    mount: str
    ads: list[dict]


def _normalize_mount(raw: str) -> str:
    mount = str(raw or "").strip()
    if not mount:
        raise HTTPException(status_code=400, detail="mount is required")
    return mount if mount.startswith("/") else f"/{mount}"


def _truthy(raw, default: bool = False) -> bool:
    token = str(raw if raw is not None else default).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _secret_is_configured(value: str) -> bool:
    stored = str(value or "").strip()
    return bool(stored) and (
        not is_credential_reference(stored)
        or bool(resolve_credential_value(stored))
    )


def _system_feature_payload(settings: dict) -> dict:
    return {
        "stream_public_base_url": str(settings.get("stream_public_base_url") or ""),
        "radio_website_url": str(settings.get("radio_website_url") or ""),
        "rocket_admin_user": str(settings.get("rocket_admin_user") or "admin"),
        "rocket_admin_password_set": _secret_is_configured(
            settings.get("rocket_admin_password", "")
        ),
        "rocket_health_password_set": _secret_is_configured(
            settings.get("rocket_health_password", "")
        ),
        "rocket_status_page_enabled": _truthy(
            settings.get("rocket_status_page_enabled", "true"), True
        ),
        "rocket_hls_enabled": _truthy(
            settings.get("rocket_hls_enabled", "true"), True
        ),
        "rocket_fallbacks_enabled": _truthy(
            settings.get("rocket_fallbacks_enabled", "true"), True
        ),
        "rocket_listener_auth_enabled": _truthy(
            settings.get("rocket_listener_auth_enabled", "false"), False
        ),
        "rocket_ad_insertion_enabled": rocket_ad_insertion_enabled_from_settings(
            settings
        ),
        "rocket_access_log_enabled": _truthy(
            settings.get("rocket_access_log_enabled", "true"), True
        ),
        "rocket_playlist_log_enabled": _truthy(
            settings.get("rocket_playlist_log_enabled", "true"), True
        ),
        "server_side_config_required": [
            "Enable the Rocket status and health endpoints in the origin configuration.",
            "Enable HLS only on the mounts that should publish it.",
            "Configure fallback mounts or files at the origin.",
            "Configure listener-auth webhooks before enforcing private streams.",
        ],
    }


def _extra_outputs(settings: dict, station_id: int) -> list[dict]:
    raw = str(
        settings.get(f"station_{int(station_id)}_extra_icecast_outputs", "") or ""
    )
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _redact_output(output: dict) -> dict:
    redacted = {
        key: value
        for key, value in dict(output).items()
        if key not in {"password", "icecast_password"}
    }
    password = str(
        output.get("icecast_password") or output.get("password") or ""
    )
    redacted["icecast_password_configured"] = bool(password)
    return redacted


def _model_dict(value) -> dict:
    serializer = getattr(value, "model_dump", None)
    if callable(serializer):
        return dict(serializer())
    serializer = getattr(value, "dict", None)
    if callable(serializer):
        return dict(serializer())
    return dict(value or {})


def _quality_channels_payload(conn, settings: dict | None = None) -> list[dict]:
    settings_repo = SettingsRepository(conn)
    settings = dict(settings or settings_repo.get_system())
    output_repo = StationOutputRepository(conn)
    stations = []
    for item in StationRepository(conn).list_all():
        station = dict(item)
        station_output = output_repo.get(int(station["id"]))
        station["_icecast_mount"] = (
            str(station_output["icecast_mount"] or "")
            if station_output is not None
            else ""
        )
        stations.append(station)
    matched = match_music_channels(stations)
    payload = []
    for channel in QUALITY_CHANNELS:
        if channel.external:
            raw = settings.get(external_settings_key(channel.channel_id), "")
            existing = parse_outputs(raw)
            item = public_channel_payload(
                    channel,
                    variants=quality_variant_state(channel, existing),
                    station_id=None,
                    credential_configured=False,
                    credential_status="managed_by_external_supervisor",
                    applied_by="external_ai_supervisor",
                )
            payload.append(item)
            continue

        station = matched.get(channel.channel_id)
        station_id = int(station["id"]) if station is not None else None
        existing = (
            _extra_outputs(settings, station_id) if station_id is not None else []
        )
        output = output_repo.get(station_id) if station_id is not None else None
        credential_ready = bool(
            output is not None
            and _secret_is_configured(str(output["icecast_password"] or ""))
        )
        item = public_channel_payload(
            channel,
            variants=quality_variant_state(channel, existing),
            station_id=station_id,
            credential_configured=credential_ready,
            credential_status="ready" if credential_ready else "missing",
            applied_by="onair_station_runtime",
        )
        item["station_found"] = station is not None
        item["legacy_output_enabled"] = bool(
            output is not None and output["icecast_enabled"]
        )
        item["primary"] = {
            "mount": channel.base_mount,
            "enabled": bool(output is not None and output["icecast_enabled"]),
            "codec": "Opus"
            if output is not None
            and str(output["stream_codec_profile"] or "").startswith("opus_")
            else "Other",
            "stream_codec_profile": (
                str(output["stream_codec_profile"] or "") if output is not None else ""
            ),
            "bitrate_kbps": (
                int(output["stream_bitrate_kbps"] or 0) if output is not None else 0
            ),
        }
        payload.append(item)
    return payload


def _quality_runtime_diagnostics(channels: list[dict]) -> list[dict]:
    try:
        from app.api.runtime import runtime_registry
    except Exception:
        runtime_registry = None
    diagnostics = []
    for channel in channels:
        if channel.get("external"):
            diagnostics.append(
                {
                    "channel_id": channel["channel_id"],
                    "owner": "external_ai_supervisor",
                    "runtime_checked": False,
                    "reason": "external_runtime",
                }
            )
            continue
        station_id = channel.get("station_id")
        primary = dict(channel.get("primary") or {})
        expected = (["icecast"] if primary.get("enabled") else []) + [
            f"icecast:{variant['mount']}"
            for variant in channel.get("variants", [])
            if variant.get("enabled")
        ]
        if runtime_registry is None or station_id is None:
            diagnostics.append(
                {
                    "channel_id": channel["channel_id"],
                    "station_id": station_id,
                    "owner": "onair_station_runtime",
                    "runtime_checked": False,
                    "expected_branches": expected,
                    "reason": "runtime_unavailable",
                }
            )
            continue
        try:
            status = dict(runtime_registry.status(int(station_id)) or {})
            delivery_health = dict(status.get("delivery_health") or {})
            diagnostics.append(
                {
                    "channel_id": channel["channel_id"],
                    "station_id": int(station_id),
                    "owner": "onair_station_runtime",
                    "runtime_checked": True,
                    "runtime_running": bool(status.get("running")),
                    "expected_branches": expected,
                    "healthy_branches": [
                        branch
                        for branch in expected
                        if delivery_health.get(branch) is True
                    ],
                    "unhealthy_branches": [
                        branch
                        for branch in expected
                        if delivery_health.get(branch) is not True
                    ],
                    "health_basis": "verified_delivery",
                }
            )
        except Exception:
            diagnostics.append(
                {
                    "channel_id": channel["channel_id"],
                    "station_id": int(station_id),
                    "owner": "onair_station_runtime",
                    "runtime_checked": False,
                    "expected_branches": expected,
                    "reason": "runtime_status_failed",
                }
            )
    return diagnostics


def _origin_capacity_diagnostics(
    configured_capacity: int,
    runtime_diagnostics: list[dict],
    enabled_local_mount_count: int,
) -> dict:
    local = [
        item
        for item in runtime_diagnostics
        if item.get("owner") == "onair_station_runtime"
    ]
    checked = [item for item in local if item.get("runtime_checked")]
    expected_mounts = sum(len(item.get("expected_branches") or []) for item in local)
    healthy_mounts = sum(len(item.get("healthy_branches") or []) for item in checked)
    unhealthy_mounts = sum(len(item.get("unhealthy_branches") or []) for item in checked)
    runtime_complete = bool(local) and len(checked) == len(local)
    observation_complete = bool(
        runtime_complete
        and expected_mounts == int(enabled_local_mount_count)
        and healthy_mounts == int(enabled_local_mount_count)
        and unhealthy_mounts == 0
    )
    configured_sufficient = int(configured_capacity) >= REQUIRED_ORIGIN_SOURCE_SLOTS
    verified = bool(configured_sufficient and observation_complete)
    if not configured_sufficient:
        warning = (
            "Declared origin source capacity is below the required "
            f"{REQUIRED_ORIGIN_SOURCE_SLOTS} slots."
        )
    elif not runtime_complete:
        warning = "Not every local station runtime could be checked for delivered audio."
    elif not observation_complete:
        warning = (
            f"Origin accepted {healthy_mounts} of {enabled_local_mount_count} "
            "enabled local mounts; rejected mounts remain on automatic retry."
        )
    else:
        warning = ""
    return {
        "required_source_slots": REQUIRED_ORIGIN_SOURCE_SLOTS,
        "recommended_source_slots": RECOMMENDED_ORIGIN_SOURCE_SLOTS,
        # Kept for API compatibility; this is an operator-declared value, not
        # proof that the origin accepted the sources.
        "configured_source_slots": int(configured_capacity) or None,
        "configured_sufficient": configured_sufficient,
        "observed_enabled_local_mounts": int(enabled_local_mount_count),
        "observed_expected_local_mounts": expected_mounts,
        "observed_healthy_local_mounts": healthy_mounts,
        "observed_unhealthy_local_mounts": unhealthy_mounts,
        "runtime_station_count": len(local),
        "runtime_checked_station_count": len(checked),
        "verification_basis": "verified_mount_delivery",
        "verified": verified,
        "warning": warning,
    }


def _refresh_quality_music_runtimes(channels: list[dict]) -> list[dict]:
    try:
        from app.api.runtime import runtime_registry
    except Exception:
        return [
            {
                "channel_id": channel["channel_id"],
                "ok": False,
                "error_code": "runtime_unavailable",
            }
            for channel in channels
            if not channel.get("external")
        ]
    results = []
    for channel in channels:
        station_id = channel.get("station_id")
        if channel.get("external") or station_id is None:
            continue
        try:
            refresh = getattr(runtime_registry, "refresh_output_settings", None)
            if not callable(refresh):
                raise RuntimeError("runtime_output_refresh_unavailable")
            refreshed = dict(refresh(int(station_id)) or {})
            if refreshed.get("producer_preserved") is False:
                raise RuntimeError("runtime_programme_producer_restarted")
            results.append(
                {
                    "channel_id": channel["channel_id"],
                    "station_id": int(station_id),
                    "ok": True,
                    "producer_preserved": bool(
                        refreshed.get("producer_preserved", True)
                    ),
                }
            )
        except Exception:
            results.append(
                {
                    "channel_id": channel["channel_id"],
                    "station_id": int(station_id),
                    "ok": False,
                    "error_code": "runtime_refresh_failed",
                }
            )
    return results


def _mount_credentials(
    conn,
    settings: dict,
    station_id: int,
    mount: str,
) -> dict:
    normalized_mount = _normalize_mount(mount)
    output_repo = StationOutputRepository(conn)
    if station_id > 0:
        row = output_repo.get(int(station_id))
        if row is not None and _normalize_mount(row["icecast_mount"]) == normalized_mount:
            return {
                "host": str(row["icecast_host"]),
                "port": int(row["icecast_port"]),
                "user": str(row["icecast_user"] or "source"),
                "password": str(row["icecast_password"] or ""),
            }
        for output in _extra_outputs(settings, station_id):
            raw_mount = str(
                output.get("icecast_mount") or output.get("mount") or ""
            ).strip()
            if raw_mount and _normalize_mount(raw_mount) == normalized_mount:
                return {
                    "host": str(
                        output.get("icecast_host") or output.get("host") or ""
                    ),
                    "port": int(
                        output.get("icecast_port") or output.get("port") or 80
                    ),
                    "user": str(
                        output.get("icecast_user")
                        or output.get("user")
                        or "source"
                    ),
                    "password": resolve_credential_value(
                        str(
                            output.get("icecast_password")
                            or output.get("password")
                            or ""
                        )
                    ),
                }

    rows = conn.execute(
        "SELECT station_id FROM station_outputs ORDER BY station_id"
    ).fetchall()
    for output_row in rows:
        row = output_repo.get(int(output_row["station_id"]))
        if row is not None and _normalize_mount(row["icecast_mount"]) == normalized_mount:
            return {
                "host": str(row["icecast_host"]),
                "port": int(row["icecast_port"]),
                "user": str(row["icecast_user"] or "source"),
                "password": str(row["icecast_password"] or ""),
            }

    return {
        "host": str(settings.get("rocket_admin_host") or "127.0.0.1"),
        "port": int(float(settings.get("rocket_admin_port") or 8000)),
        "user": str(settings.get("rocket_admin_user") or "admin"),
        "password": resolve_credential_value(
            str(settings.get("rocket_admin_password") or "")
        ),
    }


def _basic_auth(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request_text(
    url: str,
    user: str,
    password: str,
    data: dict | None = None,
) -> dict:
    encoded = None if data is None else urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        method="GET" if encoded is None else "POST",
        headers={"Authorization": _basic_auth(user, password)},
    )
    try:
        with urlopen(request, timeout=8) as response:
            body = response.read(2000).decode("utf-8", errors="replace")
            return {"ok": True, "status": int(response.status), "body": body}
    except HTTPError as exc:
        return {
            "ok": False,
            "status": int(exc.code),
            "error_code": (
                "credentials_rejected"
                if int(exc.code) in {401, 403}
                else "origin_request_failed"
            ),
            "message": "The streaming origin rejected the management request.",
        }
    except (URLError, TimeoutError, OSError):
        return {
            "ok": False,
            "error_code": "origin_unreachable",
            "message": "The streaming origin could not be reached.",
        }


@router.get("/api/streaming/features")
def get_streaming_features(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        settings_repo = SettingsRepository(conn)
        settings = settings_repo.get_system()
        stations = []
        for station in StationRepository(conn).list_all():
            sid = int(station["id"])
            station_settings = settings_repo.get_station(sid)
            safe_settings = {
                key: value
                for key, value in station_settings.items()
                if (key.startswith("icecast_") or key.startswith("rocket_"))
                and "password" not in key
            }
            output = StationOutputRepository(conn).get(sid)
            if output is not None:
                safe_settings["icecast_password_configured"] = bool(
                    output["icecast_password"]
                )
            stations.append(
                {
                    "id": sid,
                    "name": str(station["name"] or ""),
                    "settings": safe_settings,
                    "extra_icecast_outputs": [
                        _redact_output(item)
                        for item in _extra_outputs(settings, sid)
                    ],
                }
            )
        return {"system": _system_feature_payload(settings), "stations": stations}
    finally:
        conn.close()


@router.put("/api/streaming/features")
def update_streaming_features(
    payload: StreamingFeatureSettingsUpdate,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        repo = SettingsRepository(conn)
        existing = repo.get_system()
        admin_password = str(existing.get("rocket_admin_password") or "")
        health_password = str(existing.get("rocket_health_password") or "")
        if payload.rocket_admin_password:
            admin_password = store_system_secret(
                "rocket_admin_password",
                payload.rocket_admin_password,
            )
        if payload.rocket_health_password:
            health_password = store_system_secret(
                "rocket_health_password",
                payload.rocket_health_password,
            )
        repo.upsert_system(
            {
                "stream_public_base_url": payload.stream_public_base_url,
                "radio_website_url": payload.radio_website_url,
                "rocket_admin_user": payload.rocket_admin_user,
                "rocket_admin_password": admin_password,
                "rocket_health_password": health_password,
                "rocket_status_page_enabled": str(
                    bool(payload.rocket_status_page_enabled)
                ).lower(),
                "rocket_hls_enabled": str(bool(payload.rocket_hls_enabled)).lower(),
                "rocket_fallbacks_enabled": str(
                    bool(payload.rocket_fallbacks_enabled)
                ).lower(),
                "rocket_listener_auth_enabled": str(
                    bool(payload.rocket_listener_auth_enabled)
                ).lower(),
                "rocket_ad_insertion_enabled": str(
                    bool(payload.rocket_ad_insertion_enabled)
                ).lower(),
                "rocket_access_log_enabled": str(
                    bool(payload.rocket_access_log_enabled)
                ).lower(),
                "rocket_playlist_log_enabled": str(
                    bool(payload.rocket_playlist_log_enabled)
                ).lower(),
            }
        )
        return {"ok": True}
    finally:
        conn.close()


@router.get("/api/streaming/quality-outputs")
def get_quality_outputs(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    """Return all six provisioned quality channels and secret-free settings."""
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        return {
            "default_quality": "normal",
            "legacy_mounts_immutable": False,
            "primary_mounts_operator_managed": True,
            "local_mount_count": LOCAL_MUSIC_MOUNT_COUNT,
            "external_ai_mount_count": EXTERNAL_AI_MOUNT_COUNT,
            "system_mount_count": SYSTEM_MOUNT_COUNT,
            "origin_source_capacity": int(
                float(settings.get("quality_outputs_origin_source_capacity") or 0)
            ),
            "single_program_timeline": True,
            "compliance_counting": "one_play_with_delivered_variants",
            "channels": _quality_channels_payload(conn, settings),
        }
    finally:
        conn.close()


@router.get("/api/streaming/quality-outputs/diagnostics")
def diagnose_quality_outputs(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    """Return secret-free configuration, bridge, capacity, and runtime checks."""
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        channels = _quality_channels_payload(conn, settings)
        configured_capacity = int(
            float(settings.get("quality_outputs_origin_source_capacity") or 0)
        )
        enabled_opus_mounts = sum(
            1
            for channel in channels
            for variant in channel.get("variants") or []
            if variant.get("enabled") and variant.get("quality") != "flac"
        )
        encoder_capability = inspect_opus_encoder()
        encoder_capability["required_by_enabled_mounts"] = enabled_opus_mounts
        configuration_issues = []
        for channel in channels:
            if not channel.get("external") and not channel.get("station_found"):
                configuration_issues.append(
                    f"{channel['channel_id']}:station_mapping_missing"
                )
            if channel.get("credential_status") == "missing":
                configuration_issues.append(
                    f"{channel['channel_id']}:legacy_source_credential_missing"
                )
            if not channel.get("external") and channel.get("station_found"):
                primary = dict(channel.get("primary") or {})
                if not primary.get("enabled"):
                    configuration_issues.append(
                        f"{channel['channel_id']}:primary_mount_disabled"
                    )
                if primary.get("stream_codec_profile") != "opus_192":
                    configuration_issues.append(
                        f"{channel['channel_id']}:primary_mount_not_opus_192"
                    )
        if enabled_opus_mounts and not encoder_capability.get("available"):
            configuration_issues.append("opus_encoder_unavailable")
        bridge = inspect_quality_bridge()
        if not bridge.get("ok"):
            configuration_issues.append(str(bridge.get("error_code") or "bridge_invalid"))
        runtime_diagnostics = _quality_runtime_diagnostics(channels)
        enabled_local_mount_count = sum(
            1
            for channel in channels
            if dict(channel.get("primary") or {}).get("enabled")
        ) + sum(
            1
            for channel in channels
            for variant in channel.get("variants") or []
            if variant.get("enabled")
        )
        origin_capacity = _origin_capacity_diagnostics(
            configured_capacity,
            runtime_diagnostics,
            enabled_local_mount_count,
        )
        return {
            "ok": not configuration_issues,
            "channel_count": len(channels),
            "canonical_mount_count": sum(
                len(channel.get("variants") or []) for channel in channels
            ),
            "local_mount_count": LOCAL_MUSIC_MOUNT_COUNT,
            "system_mount_count": SYSTEM_MOUNT_COUNT,
            "enabled_mount_count": sum(
                1
                for channel in channels
                for variant in channel.get("variants") or []
                if variant.get("enabled")
            ),
            "enabled_local_mount_count": enabled_local_mount_count,
            "configuration_issues": configuration_issues,
            "external_bridge": bridge,
            "opus_encoder": encoder_capability,
            "origin_capacity": origin_capacity,
            "runtime": runtime_diagnostics,
            "credentials_exposed": False,
            "primary_mounts_operator_managed": True,
        }
    finally:
        conn.close()


@router.put("/api/streaming/quality-outputs")
def update_quality_outputs(
    payload: QualityOutputsSettingsUpdate,
    _user=Depends(require_permission("stations.edit")),
):
    """Save canonical quality variants without persisting duplicate secrets."""
    init_db()
    conn = get_connection()
    try:
        settings_repo = SettingsRepository(conn)
        settings = settings_repo.get_system()
        output_repo = StationOutputRepository(conn)
        stations = []
        for item in StationRepository(conn).list_all():
            station = dict(item)
            station_output = output_repo.get(int(station["id"]))
            station["_icecast_mount"] = (
                str(station_output["icecast_mount"] or "")
                if station_output is not None
                else ""
            )
            stations.append(station)
        matched = match_music_channels(stations)
        updates: dict[str, str] = {}
        if payload.origin_source_capacity is not None:
            capacity = int(payload.origin_source_capacity)
            if capacity < REQUIRED_ORIGIN_SOURCE_SLOTS or capacity > 512:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "origin source capacity must be between "
                        f"{REQUIRED_ORIGIN_SOURCE_SLOTS} and 512"
                    ),
                )
            updates["quality_outputs_origin_source_capacity"] = str(capacity)
        seen: set[str] = set()
        changed_channels: list[dict] = []
        for requested in payload.channels:
            channel_id = str(requested.channel_id or "").strip().lower()
            if channel_id in seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"duplicate quality channel: {channel_id}",
                )
            seen.add(channel_id)
            channel = QUALITY_CHANNEL_BY_ID.get(channel_id)
            if channel is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown quality channel: {channel_id}",
                )
            variants = {
                str(suffix).strip().lower(): _model_dict(value)
                for suffix, value in dict(requested.variants or {}).items()
            }
            try:
                if channel.external:
                    key = external_settings_key(channel.channel_id)
                    existing = parse_outputs(settings.get(key, ""))
                    outputs = replace_quality_outputs(channel, existing, variants)
                    updates[key] = serialized_outputs(outputs)
                    station_id = None
                else:
                    station = matched.get(channel.channel_id)
                    if station is None:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"station mapping unavailable for quality channel: "
                                f"{channel.channel_id}"
                            ),
                        )
                    station_id = int(station["id"])
                    key = f"station_{station_id}_extra_icecast_outputs"
                    existing = parse_outputs(settings.get(key, ""))
                    outputs = replace_quality_outputs(channel, existing, variants)
                    updates[key] = serialized_outputs(outputs)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            changed_channels.append(
                {
                    "channel_id": channel.channel_id,
                    "station_id": station_id,
                    "enabled_variants": [
                        item["quality"]
                        for item in outputs
                        if item.get("quality") and bool(item.get("enabled", True))
                    ],
                }
            )

        if updates:
            settings_repo.upsert_system(updates)
            for change in changed_channels:
                LogRepository(conn).add_operation_log(
                    change["station_id"],
                    "Quality output settings saved",
                    event_type="quality_outputs_updated",
                    payload=change,
                )
        readback = settings_repo.get_system()
        try:
            external_bridge = write_quality_bridge(readback)
        except Exception:
            external_bridge = {
                "ok": False,
                "error_code": "quality_output_bridge_write_failed",
            }
        return {
            "ok": bool(external_bridge.get("ok")),
            "saved_channels": [item["channel_id"] for item in changed_channels],
            "credentials_persisted": False,
            "primary_mounts_changed": False,
            "apply_behavior": (
                "Music station outputs apply on the next program item or a station "
                "runtime refresh. English and French stay on their legacy AI mounts."
            ),
            "external_bridge": external_bridge,
            "channels": _quality_channels_payload(conn, readback),
        }
    finally:
        conn.close()


@router.post("/api/streaming/quality-outputs/apply")
def apply_quality_outputs(
    payload: QualityOutputsApplyRequest,
    _user=Depends(require_permission("stations.edit")),
):
    """Apply saved quality outputs without copying credentials or touching AI mounts."""
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        channels = _quality_channels_payload(conn, settings)
        enabled_opus_mounts = sum(
            1
            for channel in channels
            for variant in channel.get("variants") or []
            if variant.get("enabled") and variant.get("quality") != "flac"
        )
        encoder_capability = inspect_opus_encoder()
        if enabled_opus_mounts and not encoder_capability.get("available"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Opus quality outputs require the FFmpeg libopus encoder. "
                    "The configured FFmpeg runtime does not provide it; no quality "
                    "outputs were applied."
                ),
            )
        try:
            bridge = write_quality_bridge(settings)
        except Exception:
            bridge = {
                "ok": False,
                "error_code": "quality_output_bridge_write_failed",
            }
        music = _refresh_quality_music_runtimes(channels)
        ai = {
            "requested": bool(payload.restart_ai_supervisor),
            "ok": bool(bridge.get("ok")),
            "restarted": False,
            "reason": "restart_not_requested",
        }
        if payload.restart_ai_supervisor and bridge.get("ok"):
            try:
                from app.services.radiotedu_service_control import (
                    SETTINGS_KEY as SERVICE_CONTROL_SETTINGS_KEY,
                    load_settings as load_service_control_settings,
                    perform_action as perform_service_control_action,
                )

                service_settings = load_service_control_settings(
                    settings.get(SERVICE_CONTROL_SETTINGS_KEY, "")
                )
                supervisor = service_settings.get("rtai_supervisor", {})
                if not supervisor.get("enabled"):
                    ai.update(
                        {
                            "ok": True,
                            "reason": "ai_supervisor_not_enabled_in_service_control",
                        }
                    )
                else:
                    perform_service_control_action(
                        "rtai_supervisor",
                        "restart",
                        "RESTART SERVICE",
                        service_settings,
                    )
                    ai.update({"ok": True, "restarted": True, "reason": ""})
            except HTTPException as exc:
                ai.update(
                    {
                        "ok": False,
                        "reason": "ai_supervisor_restart_failed",
                        "error_code": str(exc.detail or "service_control_failed"),
                    }
                )
            except Exception:
                ai.update(
                    {
                        "ok": False,
                        "reason": "ai_supervisor_restart_failed",
                        "error_code": "service_control_failed",
                    }
                )
        LogRepository(conn).add_operation_log(
            None,
            "Quality outputs applied",
            event_type="quality_outputs_applied",
            payload={
                "music_channels": [
                    {
                        "channel_id": item["channel_id"],
                        "ok": bool(item.get("ok")),
                    }
                    for item in music
                ],
                "ai_supervisor_restarted": bool(ai.get("restarted")),
                "bridge_verified": bool(bridge.get("ok")),
            },
        )
        ok = bool(
            bridge.get("ok")
            and all(item.get("ok") for item in music)
            and ai.get("ok")
        )
        return {
            "ok": ok,
            "music": music,
            "ai": ai,
            "external_bridge": bridge,
            "credentials_persisted": False,
            "primary_mounts_changed": False,
            "diagnostics": {
                "runtime": _quality_runtime_diagnostics(channels),
                "bridge": inspect_quality_bridge(),
            },
        }
    finally:
        conn.close()


@router.get("/api/streaming/health")
def rocket_health(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        host = str(settings.get("rocket_admin_host") or "127.0.0.1")
        port = int(float(settings.get("rocket_admin_port") or 8000))
        user = str(
            settings.get("rocket_health_user")
            or settings.get("rocket_admin_user")
            or "admin"
        )
        password = resolve_credential_value(
            str(
                settings.get("rocket_health_password")
                or settings.get("rocket_admin_password")
                or ""
            )
        )
        return _request_text(f"http://{host}:{port}/health", user, password)
    finally:
        conn.close()


def _management_request(payload, path: str, data: dict | None = None) -> dict:
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        mount = _normalize_mount(payload.mount)
        credentials = _mount_credentials(
            conn,
            settings,
            int(payload.station_id),
            mount,
        )
        url = f"http://{credentials['host']}:{credentials['port']}/{path}"
        return _request_text(
            url,
            credentials["user"],
            credentials["password"],
            data,
        )
    finally:
        conn.close()


@router.post("/api/streaming/metadata")
def update_stream_metadata(
    payload: MetadataUpdatePayload,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        mount = _normalize_mount(payload.mount)
        credentials = _mount_credentials(
            conn,
            settings,
            int(payload.station_id),
            mount,
        )
        query = urlencode(
            {"mode": "updinfo", "mount": mount, "song": str(payload.song or "")}
        )
        url = (
            f"http://{credentials['host']}:{credentials['port']}"
            f"/admin/metadata?{query}"
        )
        return _request_text(
            url,
            credentials["user"],
            credentials["password"],
        )
    finally:
        conn.close()


@router.post("/api/streaming/manage/move-listeners")
def move_listeners(
    payload: MoveListenersPayload,
    _user=Depends(require_permission("stations.edit")),
):
    mount = _normalize_mount(payload.mount)
    return _management_request(
        payload,
        f"{mount.strip('/')}/manage",
        {
            "action": "movelisteners",
            "dest": _normalize_mount(payload.destination),
        },
    )


@router.post("/api/streaming/manage/kick")
def kick_source(
    payload: KickSourcePayload,
    _user=Depends(require_permission("stations.edit")),
):
    mount = _normalize_mount(payload.mount)
    return _management_request(
        payload,
        f"{mount.strip('/')}/manage",
        {"action": "kick"},
    )


@router.post("/api/streaming/manage/midroll")
def insert_midroll(
    payload: MidrollPayload,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        if not rocket_ad_insertion_enabled_from_settings(settings):
            raise HTTPException(status_code=409, detail="ads_disabled_for_station")
        if int(payload.station_id) > 0:
            station_settings = SettingsRepository(conn).get_station(
                int(payload.station_id)
            )
            if not rocket_ad_insertion_enabled_from_settings(station_settings):
                raise HTTPException(
                    status_code=409,
                    detail="ads_disabled_for_station",
                )
    finally:
        conn.close()
    mount = _normalize_mount(payload.mount)
    return _management_request(
        payload,
        f"{mount.strip('/')}/manage",
        {
            "action": "midroll",
            "json": json.dumps({"ads": payload.ads}, ensure_ascii=False),
        },
    )
