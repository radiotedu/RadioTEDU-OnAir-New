from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_any_permission, require_permission, user_has_permission
from app.audio.device_registry import DeviceRegistry
from app.config import MAX_LOCAL_OUTPUTS
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository

router = APIRouter()


class StationOutputUpdate(BaseModel):
    station_id: int
    local_output_enabled: bool = False
    output_device_id: str = ""
    icecast_enabled: bool = False
    icecast_host: str = "127.0.0.1"
    icecast_port: int = 8000
    icecast_mount: str = "/stream"
    icecast_user: str = "source"
    icecast_password: str = ""
    icecast_tls_enabled: bool = False
    output_gain_db: float = 0.0
    stream_codec_profile: str = "opus_192"
    stream_bitrate_kbps: int = 192
    source_protocol: str = "icecast"


def _normalize_stream_profile(raw_profile: str, raw_bitrate: int) -> tuple[str, int]:
    token = str(raw_profile or "").strip().lower().replace("-", "_").replace("+", "_plus_")
    aliases = {
        "mp3": ("mp3_128", 128),
        "mp3_128": ("mp3_128", 128),
        "mp3_128kbps": ("mp3_128", 128),
        "aac": ("aac_192", 192),
        "aac_192": ("aac_192", 192),
        "aac_lc_196": ("aac_lc_196", 196),
        "aac_lc_196kbps": ("aac_lc_196", 196),
        # Backward-compatible aliases. The bundled FFmpeg AAC encoder produces
        # AAC-LC, not HE-AAC/AAC+, so persist the truthful profile name.
        "aac_plus_196": ("aac_lc_196", 196),
        "aac_plus_196kbps": ("aac_lc_196", 196),
        "aacplus_196": ("aac_lc_196", 196),
        "opus_196": ("opus_196", 196),
        "opus_320": ("opus_320", 320),
        "opus_32": ("opus_32", 32),
        "opus_64": ("opus_64", 64),
        "opus_96": ("opus_96", 96),
        "opus_192": ("opus_192", 192),
    }
    if token in aliases:
        return aliases[token]
    try:
        bitrate = max(32, min(512, int(raw_bitrate)))
    except (TypeError, ValueError):
        bitrate = 192
    if token.startswith("opus_"):
        return f"opus_{max(32, min(320, bitrate))}", max(32, min(320, bitrate))
    return "opus_192", 192


def _row_to_output_payload(station_id: int, row, station_settings: dict | None = None) -> dict:
    settings = dict(station_settings or {})
    if row is None:
        return {
            "station_id": station_id,
            "local_output_enabled": True,
            "output_device_id": "",
            "icecast_enabled": False,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": f"/station{station_id}",
            "icecast_user": "source",
            "icecast_password": "",
            "icecast_password_configured": False,
            "icecast_tls_enabled": bool(
                str(settings.get("icecast_tls_enabled", "false")).strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            "output_gain_db": 0.0,
            "stream_codec_profile": "opus_192",
            "stream_bitrate_kbps": 192,
            "source_protocol": "icecast",
        }
    return {
        "station_id": station_id,
        "local_output_enabled": bool(row["local_output_enabled"]),
        "output_device_id": str(row["output_device_id"]),
        "icecast_enabled": bool(row["icecast_enabled"]),
        "icecast_host": str(row["icecast_host"]),
        "icecast_port": int(row["icecast_port"]),
        "icecast_mount": str(row["icecast_mount"]),
        "icecast_user": str(row["icecast_user"]),
        "icecast_password": "",
        "icecast_password_configured": bool(str(row["icecast_password"] or "")),
        "icecast_tls_enabled": bool(
            str(
                settings.get(
                    "icecast_tls_enabled",
                    str(int(row["icecast_port"]) == 443).lower(),
                )
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        ),
        "output_gain_db": float(row["output_gain_db"]),
        "stream_codec_profile": str(row["stream_codec_profile"] or "opus_192"),
        "stream_bitrate_kbps": int(row["stream_bitrate_kbps"] or 192),
        "source_protocol": str(row["source_protocol"] or "icecast"),
    }


@router.get("/api/stations/output")
def get_station_output(
    station_id: int,
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    init_db()
    conn = get_connection()
    repo = StationOutputRepository(conn)
    row = repo.get(station_id)
    station_settings = SettingsRepository(conn).get_station(int(station_id))
    payload = _row_to_output_payload(station_id, row, station_settings)
    return payload


@router.post("/api/stations/output")
def update_station_output(
    payload: StationOutputUpdate,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    repo = StationOutputRepository(conn)
    normalized_device_id = str(payload.output_device_id or "").strip()
    normalized_mount = str(payload.icecast_mount or "").strip()
    normalized_profile, normalized_bitrate = _normalize_stream_profile(
        payload.stream_codec_profile,
        payload.stream_bitrate_kbps,
    )
    normalized_protocol = str(payload.source_protocol or "icecast").strip().lower()
    if normalized_protocol not in {"icecast", "shoutcast"}:
        raise HTTPException(
            status_code=400,
            detail="source_protocol must be icecast or shoutcast",
        )
    if normalized_protocol == "shoutcast" and not normalized_profile.startswith(("mp3_", "aac")):
        raise HTTPException(
            status_code=400,
            detail="SHOUTcast legacy source supports MP3 or AAC profiles",
        )

    if not payload.local_output_enabled and not payload.icecast_enabled:
        raise HTTPException(
            status_code=400, detail="at least one output target must be enabled"
        )

    if payload.local_output_enabled and normalized_device_id:
        registry = DeviceRegistry(max_local_outputs=MAX_LOCAL_OUTPUTS)
        for row in repo.list_active_local_output_assignments(
            exclude_station_id=payload.station_id
        ):
            registry.assign(
                station_id=int(row["station_id"]),
                device_id=str(row["output_device_id"]),
            )
        ok, reason = registry.validate_assignment(
            station_id=payload.station_id,
            device_id=normalized_device_id,
        )
        if not ok:
            if reason == "max_local_outputs":
                raise HTTPException(status_code=409, detail="max local outputs limit reached")
            if reason == "device_in_use":
                raise HTTPException(status_code=409, detail="output device already assigned")
            raise HTTPException(status_code=400, detail="invalid local output device")

    if payload.icecast_enabled and normalized_protocol == "icecast" and not normalized_mount:
        raise HTTPException(
            status_code=400,
            detail="icecast_mount is required when icecast is enabled",
        )

    if payload.icecast_enabled:
        conflict = repo.find_active_stream_conflict(
            station_id=int(payload.station_id),
            host=str(payload.icecast_host or "").strip(),
            port=int(payload.icecast_port),
            mount=normalized_mount or str(payload.icecast_mount or "").strip(),
            source_protocol=normalized_protocol,
        )
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "stream destination already assigned to station "
                    f"{int(conflict['station_id'])}"
                ),
            )

    repo.upsert(
        station_id=payload.station_id,
        local_output_enabled=payload.local_output_enabled,
        output_device_id=normalized_device_id,
        icecast_enabled=payload.icecast_enabled,
        icecast_host=payload.icecast_host,
        icecast_port=payload.icecast_port,
        icecast_mount=normalized_mount or payload.icecast_mount,
        icecast_user=payload.icecast_user,
        icecast_password=payload.icecast_password,
        output_gain_db=payload.output_gain_db,
        stream_codec_profile=normalized_profile,
        stream_bitrate_kbps=normalized_bitrate,
        source_protocol=normalized_protocol,
    )
    SettingsRepository(conn).upsert_station(
        int(payload.station_id),
        {
            "output_mode": normalized_protocol if payload.icecast_enabled else "speaker",
            "source_protocol": normalized_protocol,
            "speaker_monitor_enabled": str(bool(payload.local_output_enabled)).lower(),
            "output_device_id": normalized_device_id,
            "icecast_host": str(payload.icecast_host or "").strip(),
            "icecast_port": str(int(payload.icecast_port)),
            "icecast_mount": normalized_mount or str(payload.icecast_mount or "").strip(),
            "icecast_username": str(payload.icecast_user or "").strip(),
            # The shared settings table must never contain stream passwords.
            # StationOutputRepository stores the secret in the per-user vault.
            "icecast_password": "",
            "icecast_tls_enabled": str(bool(payload.icecast_tls_enabled)).lower(),
            "output_gain_db": str(float(payload.output_gain_db)),
            "stream_codec_profile": normalized_profile,
            "stream_bitrate_kbps": str(normalized_bitrate),
        },
    )
    station_settings = SettingsRepository(conn).get_station(int(payload.station_id))
    return {
        "ok": True,
        "output": _row_to_output_payload(
            payload.station_id,
            repo.get(payload.station_id),
            station_settings,
        ),
    }
