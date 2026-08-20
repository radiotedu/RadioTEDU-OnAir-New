from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import require_any_permission, require_permission
from app.services.stream_config_service import StreamConfigError, stream_config_service

router = APIRouter()


class StreamDraftPayload(BaseModel):
    station_id: int
    local_output_enabled: bool = False
    output_device_id: str = ""
    icecast_enabled: bool = True
    icecast_host: str
    icecast_port: int = Field(default=8000, ge=1, le=65535)
    icecast_mount: str
    icecast_user: str = "source"
    icecast_password: str = ""
    icecast_tls_enabled: bool = False
    output_gain_db: float = Field(default=0, ge=-30, le=12)
    stream_codec_profile: str = "opus_192"
    source_protocol: str = "icecast"


class ApplyPayload(BaseModel):
    override_reason: str = ""
    defer_listener_verification: bool = False


def _raise(exc: Exception):
    detail = str(exc) or "stream_configuration_failed"
    status = 404 if detail.endswith("not_found") else (409 if "not_" in detail or "unsafe" in detail or "needs_attention" in detail or "ha_" in detail else 400)
    raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/api/stream-config/drafts")
def create_stream_draft(payload: StreamDraftPayload, user=Depends(require_permission("stream.configure_basic"))):
    try:
        permissions = set(user.get("effective_permissions") or [])
        allow_advanced = str(user.get("role") or "") in {"admin", "superadmin"} or "stream.configure_advanced" in permissions
        return stream_config_service.create_draft(
            payload.model_dump(),
            actor_id=int(user["id"]),
            allow_advanced=allow_advanced,
        )
    except (StreamConfigError, ValueError) as exc:
        _raise(exc)


@router.get("/api/stream-config/drafts/{draft_id}")
def get_stream_draft(draft_id: int, _user=Depends(require_any_permission("stream.configure_basic", "stream.configure_advanced"))):
    try:
        return stream_config_service.get_draft(draft_id)
    except StreamConfigError as exc:
        _raise(exc)


@router.post("/api/stream-config/drafts/{draft_id}/validate")
def validate_stream_draft(draft_id: int, _user=Depends(require_permission("stream.configure_basic"))):
    try:
        return stream_config_service.validate(draft_id)
    except StreamConfigError as exc:
        _raise(exc)


@router.post("/api/stream-config/drafts/{draft_id}/apply")
def apply_stream_draft(
    draft_id: int,
    payload: ApplyPayload,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    user=Depends(require_permission("stream.configure_basic")),
):
    permissions = set(user.get("effective_permissions") or [])
    is_advanced = (
        str(user.get("role") or "") in {"admin", "superadmin"}
        or "stream.configure_advanced" in permissions
    )
    if payload.override_reason and not is_advanced:
        raise HTTPException(status_code=403, detail="advanced_permission_required")
    try:
        return stream_config_service.apply(
            draft_id,
            actor_id=int(user["id"]),
            idempotency_key=idempotency_key,
            override_reason=payload.override_reason,
            defer_listener_verification=payload.defer_listener_verification,
        )
    except (StreamConfigError, RuntimeError, ValueError) as exc:
        _raise(exc)


@router.get("/api/stream-config/operations/{operation_id}")
def get_stream_operation(operation_id: int, _user=Depends(require_any_permission("stream.configure_basic", "stream.configure_advanced"))):
    try:
        return stream_config_service.operation(operation_id)
    except StreamConfigError as exc:
        _raise(exc)


@router.post("/api/stream-config/operations/{operation_id}/rollback")
def rollback_stream_operation(operation_id: int, user=Depends(require_permission("stream.configure_advanced"))):
    try:
        return stream_config_service.rollback(operation_id, actor_id=int(user["id"]))
    except StreamConfigError as exc:
        _raise(exc)
