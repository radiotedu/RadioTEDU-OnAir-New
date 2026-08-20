from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.audio.device_discovery import list_output_devices
from app.audio.live_mic_registry import live_mic_registry
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.services.studio_service import StudioForbiddenError, StudioService

router = APIRouter()

_DEFAULT_LIVE_AUDIO_SETTINGS = {
    "program_music_mode": "normal",
    "mic_gain": 1.0,
    "music_gain": 1.0,
    "duck_level": 0.15,
}


def _normalize_device(raw) -> dict[str, str] | None:
    if isinstance(raw, dict):
        device_id = str(
            raw.get("id")
            or raw.get("device_id")
            or raw.get("name")
            or raw.get("label")
            or ""
        ).strip()
        label = str(raw.get("label") or raw.get("name") or device_id).strip()
    else:
        device_id = str(raw or "").strip()
        label = device_id
    if not device_id:
        return None
    return {
        "id": device_id,
        "label": label or device_id,
    }


def _request_user(request: Request) -> dict:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
    }


def _normalize_program_music_mode(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token in {"duck", "mute", "normal"}:
        return token
    return _DEFAULT_LIVE_AUDIO_SETTINGS["program_music_mode"]


def _normalize_gain(raw: Any, default: float) -> float:
    try:
        return max(0.0, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return float(default)


def _normalize_duck_level(raw: Any) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return float(_DEFAULT_LIVE_AUDIO_SETTINGS["duck_level"])


def _settings_snapshot(repo: SettingsRepository, station_id: int) -> dict[str, Any]:
    station_settings = repo.get_station(int(station_id))
    return {
        "station_id": int(station_id),
        "live_input_enabled": False,
        "transmitting": False,
        "active_user": None,
        "program_music_mode": _normalize_program_music_mode(
            station_settings.get("program_music_mode")
        ),
        "mic_gain": _normalize_gain(
            station_settings.get("program_mic_gain"),
            _DEFAULT_LIVE_AUDIO_SETTINGS["mic_gain"],
        ),
        "music_gain": _normalize_gain(
            station_settings.get("program_music_gain"),
            _DEFAULT_LIVE_AUDIO_SETTINGS["music_gain"],
        ),
        "duck_level": _normalize_duck_level(
            station_settings.get("program_duck_level")
        ),
    }


def _require_live_input_owner(station_id: int, actor: dict) -> None:
    init_db()
    conn = get_connection()
    try:
        allowed, detail = StudioService(conn).can_user_activate_mic(
            int(station_id),
            int(actor["id"]),
        )
    finally:
        conn.close()
    if not allowed:
        raise HTTPException(status_code=403, detail=str(detail or "forbidden"))


def _allow_live_input_stop(station_id: int, actor: dict) -> None:
    snapshot = live_mic_registry.snapshot(int(station_id))
    active_user = dict(snapshot.get("active_user") or {})
    if int(active_user.get("id", 0) or 0) == int(actor["id"]):
        return
    _require_live_input_owner(station_id, actor)


class LiveAudioSettingsPayload(BaseModel):
    station_id: int = 1
    program_music_mode: str = "normal"
    mic_gain: float = 1.0
    music_gain: float = 1.0
    duck_level: float = 0.15


class LiveRenderStartPayload(BaseModel):
    station_id: int = 1
    source_name: str = "OmniVoice"
    input_format: str = "s16le"
    sample_rate: int = 24000
    channels: int = 1
    max_buffer_bytes: int = 480000


class LiveRenderStopPayload(BaseModel):
    station_id: int = 1


@router.get("/api/audio/devices")
def get_output_devices():
    try:
        raw_devices = list_output_devices()
        devices = [
            device for device in (_normalize_device(item) for item in raw_devices) if device
        ]
        return {"devices": devices}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"device discovery failed: {exc}") from exc


@router.get("/api/audio/live/status")
def get_live_audio_status(station_id: int = 1):
    init_db()
    conn = get_connection()
    try:
        return _settings_snapshot(SettingsRepository(conn), int(station_id))
    finally:
        conn.close()


@router.put("/api/audio/live/settings")
def update_live_audio_settings(payload: LiveAudioSettingsPayload):
    init_db()
    conn = get_connection()
    try:
        repo = SettingsRepository(conn)
        normalized = {
            "program_music_mode": _normalize_program_music_mode(
                payload.program_music_mode
            ),
            "mic_gain": _normalize_gain(
                payload.mic_gain, _DEFAULT_LIVE_AUDIO_SETTINGS["mic_gain"]
            ),
            "music_gain": _normalize_gain(
                payload.music_gain, _DEFAULT_LIVE_AUDIO_SETTINGS["music_gain"]
            ),
            "duck_level": _normalize_duck_level(payload.duck_level),
        }
        repo.upsert_station(
            int(payload.station_id),
            {
                "program_music_mode": normalized["program_music_mode"],
                "program_mic_gain": normalized["mic_gain"],
                "program_music_gain": normalized["music_gain"],
                "program_duck_level": normalized["duck_level"],
            },
        )
        try:
            from app.api.runtime import runtime_registry

            runtime_registry.refresh_live_audio_settings(int(payload.station_id))
        except Exception:
            pass
        return {
            "station_id": int(payload.station_id),
            "program_music_mode": normalized["program_music_mode"],
            "mic_gain": normalized["mic_gain"],
            "music_gain": normalized["music_gain"],
            "duck_level": normalized["duck_level"],
        }
    finally:
        conn.close()


@router.post("/api/audio/live/render/start")
def start_live_render(payload: LiveRenderStartPayload, request: Request):
    actor = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        service = StudioService(conn)
        try:
            return service.start_render_transmission(
                int(payload.station_id),
                actor,
                source_name=str(payload.source_name or "OmniVoice"),
                input_format=str(payload.input_format or "s16le"),
                sample_rate=int(payload.sample_rate or 24000),
                channels=int(payload.channels or 1),
                max_buffer_bytes=int(payload.max_buffer_bytes or 480000),
            )
        except StudioForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/api/audio/live/render/chunk")
async def push_live_render_chunk(request: Request, station_id: int = 1):
    actor = _request_user(request)
    _require_live_input_owner(int(station_id), actor)
    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="empty_render_chunk")
    try:
        return live_mic_registry.push_render_chunk(int(station_id), payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/audio/live/render/stop")
def stop_live_render(payload: LiveRenderStopPayload, request: Request):
    actor = _request_user(request)
    _allow_live_input_stop(int(payload.station_id), actor)
    return live_mic_registry.stop_render_transmission(
        int(payload.station_id),
        user_id=int(actor["id"]),
    )
