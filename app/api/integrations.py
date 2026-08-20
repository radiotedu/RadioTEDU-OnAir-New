import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.auth.dependencies import require_any_permission, require_permission
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.security.credential_vault import (
    resolve_credential_value,
    store_system_secret,
)
from app.services.radiotedu_service_control import (
    SETTINGS_KEY as SERVICE_CONTROL_SETTINGS_KEY,
    all_service_statuses,
    load_settings as load_service_control_settings,
    normalize_settings as normalize_service_control_settings,
    perform_action as perform_service_control_action,
    public_settings as public_service_control_settings,
    settings_json as service_control_settings_json,
)
from app.services.juke_library_admin import (
    list_library as list_juke_library,
    restore_item as restore_juke_library_item,
    retire_item as retire_juke_library_item,
    store_upload as store_juke_library_upload,
)

router = APIRouter()


class RadioTEDUIntegrationSettingsUpdate(BaseModel):
    voting_enabled: bool = False
    voting_base_url: str = ""
    voting_agent_device_id: str = ""
    voting_agent_token: str = ""
    study_enabled: bool = False
    study_base_url: str = ""


class VotingCandidate(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    song_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=255)
    artist: str = Field(min_length=1, max_length=255)
    album_art_url: str | None = Field(default=None, max_length=2000)


class PublishVotingRoundPayload(BaseModel):
    round_id: str = Field(default="", max_length=120)
    candidates: list[VotingCandidate] = Field(min_length=3, max_length=3)
    lock_after_seconds: int = Field(default=30, ge=5, le=300)
    resolve_after_seconds: int = Field(default=45, ge=5, le=600)


class ResolveVotingRoundPayload(BaseModel):
    round_id: str = Field(min_length=1, max_length=120)


class RadioTEDUServiceSettingsUpdate(BaseModel):
    services: dict[str, dict[str, Any]]


class RadioTEDUServiceAction(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    confirmation: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)


class JukeLibraryRetirePayload(BaseModel):
    root_id: str = Field(min_length=1, max_length=40)
    relative_path: str = Field(min_length=1, max_length=2000)
    confirmation: str = Field(default="", max_length=80)


class JukeLibraryRestorePayload(BaseModel):
    root_id: str = Field(min_length=1, max_length=40)
    trash_path: str = Field(min_length=1, max_length=2200)
    confirmation: str = Field(default="", max_length=80)


def _truthy(raw, default: bool = False) -> bool:
    token = str(raw if raw is not None else default).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _validated_base_url(raw: str, *, setting: str) -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        raise HTTPException(status_code=409, detail=f"{setting}_not_configured")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail=f"invalid_{setting}")
    if parsed.scheme != "https" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise HTTPException(status_code=400, detail=f"{setting}_requires_https")
    return value


def _request_json(
    method: str,
    url: str,
    *,
    token: str = "",
    device_id: str = "",
    payload: dict | None = None,
    timeout_seconds: float = 8.0,
) -> dict:
    body = (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if payload is not None
        else None
    )
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if device_id:
        headers["x-rt-device-id"] = device_id
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read(256_000).decode(
                "utf-8",
                errors="replace",
            )
            parsed = json.loads(response_body) if response_body else {}
            return {
                "ok": True,
                "status": int(response.status),
                "data": parsed,
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "status": int(exc.code),
            "error_code": (
                "credentials_rejected"
                if int(exc.code) in {401, 403}
                else "remote_request_rejected"
            ),
            "message": "The RadioTEDU service rejected the request.",
        }
    except (URLError, TimeoutError, OSError, ValueError):
        return {
            "ok": False,
            "status": 0,
            "error_code": "remote_unavailable",
            "message": (
                "The optional RadioTEDU service is unavailable. "
                "Core playout is unaffected."
            ),
        }


def _settings_payload(settings: dict) -> dict:
    return {
        "voting_enabled": _truthy(
            settings.get("radiotedu_voting_enabled", "false")
        ),
        "voting_base_url": str(
            settings.get("radiotedu_voting_base_url") or ""
        ),
        "voting_agent_device_id": str(
            settings.get("radiotedu_voting_agent_device_id") or ""
        ),
        "voting_agent_token_configured": bool(
            str(settings.get("radiotedu_voting_agent_token") or "")
        ),
        "study_enabled": _truthy(
            settings.get("radiotedu_study_enabled", "false")
        ),
        "study_base_url": str(
            settings.get("radiotedu_study_base_url") or ""
        ),
    }


def _load_settings() -> tuple[dict, str]:
    init_db()
    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
    finally:
        conn.close()
    token = resolve_credential_value(
        str(settings.get("radiotedu_voting_agent_token") or "")
    )
    return settings, token


def _load_service_control_settings() -> dict[str, dict[str, Any]]:
    init_db()
    conn = get_connection()
    try:
        raw = SettingsRepository(conn).get_system().get(
            SERVICE_CONTROL_SETTINGS_KEY,
            "",
        )
    finally:
        conn.close()
    return load_service_control_settings(raw)


@router.get("/api/integrations/radiotedu")
def get_radiotedu_integrations(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    settings, _token = _load_settings()
    return _settings_payload(settings)


@router.put("/api/integrations/radiotedu")
def update_radiotedu_integrations(
    payload: RadioTEDUIntegrationSettingsUpdate,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        repo = SettingsRepository(conn)
        existing = repo.get_system()
        token_value = str(
            existing.get("radiotedu_voting_agent_token") or ""
        )
        if payload.voting_agent_token:
            token_value = store_system_secret(
                "radiotedu_voting_agent_token",
                payload.voting_agent_token,
            )
        if payload.voting_enabled:
            _validated_base_url(
                payload.voting_base_url,
                setting="voting_base_url",
            )
            if not payload.voting_agent_device_id.strip():
                raise HTTPException(
                    status_code=400,
                    detail="voting_agent_device_id_required",
                )
            if not token_value:
                raise HTTPException(
                    status_code=400,
                    detail="voting_agent_token_required",
                )
        if payload.study_enabled:
            _validated_base_url(
                payload.study_base_url,
                setting="study_base_url",
            )
        repo.upsert_system(
            {
                "radiotedu_voting_enabled": str(
                    bool(payload.voting_enabled)
                ).lower(),
                "radiotedu_voting_base_url": payload.voting_base_url.strip().rstrip(
                    "/"
                ),
                "radiotedu_voting_agent_device_id": (
                    payload.voting_agent_device_id.strip()
                ),
                "radiotedu_voting_agent_token": token_value,
                "radiotedu_study_enabled": str(
                    bool(payload.study_enabled)
                ).lower(),
                "radiotedu_study_base_url": payload.study_base_url.strip().rstrip(
                    "/"
                ),
            }
        )
        return {"ok": True, **_settings_payload(repo.get_system())}
    finally:
        conn.close()


@router.get("/api/integrations/radiotedu/status")
def radiotedu_integration_status(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    settings, _token = _load_settings()
    config = _settings_payload(settings)
    voting = {
        "enabled": config["voting_enabled"],
        "state": "disabled",
        "core_playout_affected": False,
    }
    if config["voting_enabled"]:
        base_url = _validated_base_url(
            config["voting_base_url"],
            setting="voting_base_url",
        )
        result = _request_json(
            "GET",
            f"{base_url}/next-song-voting/status",
            timeout_seconds=4.0,
        )
        voting.update(
            {
                "state": "ready" if result["ok"] else "degraded",
                "result": result,
            }
        )
    study = {
        "enabled": config["study_enabled"],
        "state": "configured" if config["study_enabled"] else "disabled",
        "base_url": config["study_base_url"],
        "mode": "external_authenticated_experience",
        "core_playout_affected": False,
    }
    return {"voting": voting, "study": study}


@router.get("/api/integrations/radiotedu/services")
def get_radiotedu_services(
    refresh_health: bool = True,
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    settings = _load_service_control_settings()
    return {
        **public_service_control_settings(settings),
        "status": all_service_statuses(
            settings,
            include_health=bool(refresh_health),
        ),
    }


@router.put("/api/integrations/radiotedu/services")
def update_radiotedu_services(
    payload: RadioTEDUServiceSettingsUpdate,
    _user=Depends(require_permission("stations.edit")),
):
    settings = normalize_service_control_settings(payload.services)
    init_db()
    conn = get_connection()
    try:
        SettingsRepository(conn).upsert_system(
            {
                SERVICE_CONTROL_SETTINGS_KEY: service_control_settings_json(
                    settings
                )
            }
        )
    finally:
        conn.close()
    return {
        "ok": True,
        **public_service_control_settings(settings),
        "status": all_service_statuses(settings, include_health=False),
    }


@router.post("/api/integrations/radiotedu/services/{service_id}/action")
def control_radiotedu_service(
    service_id: str,
    payload: RadioTEDUServiceAction,
    _user=Depends(require_permission("stations.edit")),
):
    settings = _load_service_control_settings()
    result = perform_service_control_action(
        service_id,
        payload.action,
        payload.confirmation,
        settings,
        payload.model,
    )
    return {
        **result,
        "status": all_service_statuses(
            settings,
            include_health=payload.action == "check",
        ),
    }


def _juke_library_config_path() -> str:
    settings = _load_service_control_settings()
    config = settings.get("juke_media_agent") or {}
    path = str(config.get("config_path") or "").strip()
    if not path:
        raise HTTPException(status_code=409, detail="juke_config_not_ready")
    return path


@router.get("/api/integrations/radiotedu/juke-library")
def get_juke_library(
    query: str = "",
    root_id: str = "",
    include_trash: bool = False,
    limit: int = 200,
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    return list_juke_library(
        _juke_library_config_path(),
        query=query,
        root_id=root_id,
        include_trash=bool(include_trash),
        limit=limit,
    )


@router.post("/api/integrations/radiotedu/juke-library/upload")
async def upload_juke_library_items(
    root_id: str = Form(...),
    relative_folder: str = Form(""),
    files: list[UploadFile] = File(default_factory=list),
    _user=Depends(require_permission("stations.edit")),
):
    if not files:
        raise HTTPException(status_code=400, detail="no_juke_files_uploaded")
    config_path = _juke_library_config_path()
    stored: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for upload in files:
        name = str(upload.filename or "upload")
        try:
            stored.append(
                await store_juke_library_upload(
                    config_path,
                    root_id=root_id,
                    relative_folder=relative_folder,
                    upload=upload,
                )
            )
        except HTTPException as exc:
            failures.append({"file": name, "error": str(exc.detail)})
        finally:
            await upload.close()
    if not stored and failures:
        raise HTTPException(
            status_code=409,
            detail={"code": "juke_upload_failed", "files": failures},
        )
    return {
        "ok": not failures,
        "stored": stored,
        "failures": failures,
        "stored_count": len(stored),
        "failed_count": len(failures),
    }


@router.post("/api/integrations/radiotedu/juke-library/retire")
def retire_juke_library_song(
    payload: JukeLibraryRetirePayload,
    _user=Depends(require_permission("stations.edit")),
):
    if payload.confirmation != "RETIRE JUKE SONG":
        raise HTTPException(status_code=400, detail="confirmation_required")
    return retire_juke_library_item(
        _juke_library_config_path(),
        root_id=payload.root_id,
        relative_path=payload.relative_path,
    )


@router.post("/api/integrations/radiotedu/juke-library/restore")
def restore_juke_library_song(
    payload: JukeLibraryRestorePayload,
    _user=Depends(require_permission("stations.edit")),
):
    if payload.confirmation != "RESTORE JUKE SONG":
        raise HTTPException(status_code=400, detail="confirmation_required")
    return restore_juke_library_item(
        _juke_library_config_path(),
        root_id=payload.root_id,
        trash_path=payload.trash_path,
    )


@router.post("/api/integrations/radiotedu/voting/rounds")
def publish_voting_round(
    payload: PublishVotingRoundPayload,
    _user=Depends(require_permission("stations.edit")),
):
    settings, token = _load_settings()
    config = _settings_payload(settings)
    if not config["voting_enabled"]:
        raise HTTPException(status_code=409, detail="voting_disabled")
    base_url = _validated_base_url(
        config["voting_base_url"],
        setting="voting_base_url",
    )
    now = datetime.now(timezone.utc)
    lock_at = now + timedelta(seconds=int(payload.lock_after_seconds))
    resolve_at = now + timedelta(
        seconds=max(
            int(payload.resolve_after_seconds),
            int(payload.lock_after_seconds),
        )
    )
    round_id = payload.round_id.strip() or f"onair-{uuid.uuid4().hex}"
    result = _request_json(
        "POST",
        f"{base_url}/next-song-voting/agent/rounds",
        token=token,
        device_id=config["voting_agent_device_id"],
        payload={
            "id": round_id,
            "status": "open",
            "openedAt": now.isoformat(),
            "lockAt": lock_at.isoformat(),
            "resolveAt": resolve_at.isoformat(),
            "candidates": [
                {
                    "id": candidate.id,
                    "songId": candidate.song_id,
                    "title": candidate.title,
                    "artist": candidate.artist,
                    "albumArtUrl": candidate.album_art_url,
                }
                for candidate in payload.candidates
            ],
        },
    )
    return {
        "round_id": round_id,
        "state": "published" if result["ok"] else "degraded",
        "core_playout_affected": False,
        "result": result,
    }


@router.post("/api/integrations/radiotedu/voting/resolve")
def resolve_voting_round(
    payload: ResolveVotingRoundPayload,
    _user=Depends(require_permission("stations.edit")),
):
    settings, token = _load_settings()
    config = _settings_payload(settings)
    if not config["voting_enabled"]:
        raise HTTPException(status_code=409, detail="voting_disabled")
    base_url = _validated_base_url(
        config["voting_base_url"],
        setting="voting_base_url",
    )
    result = _request_json(
        "POST",
        (
            f"{base_url}/next-song-voting/agent/rounds/"
            f"{payload.round_id}/resolve"
        ),
        token=token,
        device_id=config["voting_agent_device_id"],
        payload={},
    )
    return {
        "round_id": payload.round_id,
        "state": "resolved" if result["ok"] else "degraded",
        "core_playout_affected": False,
        "result": result,
    }
