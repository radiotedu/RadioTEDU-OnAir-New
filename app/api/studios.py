from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.audio.live_mic_registry import live_mic_registry
from app.services.studio_service import (
    StudioConflictError,
    StudioForbiddenError,
    StudioNotFoundError,
    StudioService,
    StudioValidationError,
)
from app.ws.broadcaster import EventBroadcaster, broadcaster, connection_manager

router = APIRouter()


class StudioCreatePayload(BaseModel):
    station_id: int
    name: str
    description: str = ""


class StudioUpdatePayload(BaseModel):
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class StudioHandoffPayload(BaseModel):
    station_id: int
    source_studio_id: int
    target_studio_id: int


class StudioChatPayload(BaseModel):
    message: str


def _request_user(request: Request) -> dict:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
    }


def _studio_row_to_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "name": str(row["name"]),
        "description": str(row["description"] or ""),
        "sort_order": int(row["sort_order"] or 1),
        "is_active": bool(row["is_active"]),
        "is_on_air": bool(row["is_on_air"]),
        "current_user_id": (
            int(row["current_user_id"]) if row["current_user_id"] is not None else None
        ),
    }


def _translate_service_error(exc: Exception) -> None:
    if isinstance(exc, StudioNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, StudioConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, StudioForbiddenError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, StudioValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def _presence_payload(station_id: int) -> dict:
    return connection_manager.presence(EventBroadcaster.station_room(int(station_id)))


def _broadcast_station_state(service: StudioService, station_id: int) -> None:
    broadcaster.on_studio_status(
        int(station_id),
        service.snapshot_station(int(station_id)),
    )
    broadcaster.on_dj_presence(int(station_id), _presence_payload(int(station_id)))


def _enforce_live_mic_ownership(service: StudioService, station_id: int) -> None:
    snapshot = live_mic_registry.snapshot(int(station_id))
    active_user = dict(snapshot.get("active_user") or {})
    active_user_id = int(active_user.get("id", 0) or 0)
    if active_user_id <= 0:
        return
    allowed, _detail = service.can_user_activate_mic(int(station_id), active_user_id)
    if not allowed:
        live_mic_registry.stop_live_input(int(station_id), user_id=active_user_id)


@router.get("/api/studios")
def list_studios(station_id: int, request: Request):
    init_db()
    conn = get_connection()
    try:
        return StudioService(conn).snapshot_station(station_id, actor=_request_user(request))
    finally:
        conn.close()


@router.post("/api/studios")
def create_studio(payload: StudioCreatePayload, request: Request):
    init_db()
    conn = get_connection()
    try:
        service = StudioService(conn)
        actor = _request_user(request)
        try:
            studio_id = service.create_studio(
                station_id=int(payload.station_id),
                name=str(payload.name or ""),
                description=str(payload.description or ""),
                actor=actor,
            )
        except Exception as exc:
            _translate_service_error(exc)
        row = service.repo.get(studio_id)
        if row is None:
            raise HTTPException(status_code=404, detail="studio_not_found")
        _broadcast_station_state(service, int(payload.station_id))
        return {"studio": _studio_row_to_dict(row)}
    finally:
        conn.close()


@router.put("/api/studios/{studio_id}")
def update_studio(studio_id: int, payload: StudioUpdatePayload, request: Request):
    actor = _request_user(request)
    if actor["role"] != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    init_db()
    conn = get_connection()
    try:
        service = StudioService(conn)
        row = service.repo.get(int(studio_id))
        if row is None:
            raise HTTPException(status_code=404, detail="studio_not_found")
        updates = {
            "name": str(payload.name).strip() if payload.name is not None else None,
            "description": (
                str(payload.description).strip()
                if payload.description is not None
                else None
            ),
            "sort_order": int(payload.sort_order)
            if payload.sort_order is not None
            else None,
            "is_active": 1 if payload.is_active is True else 0 if payload.is_active is False else None,
        }
        if updates["name"] == "":
            raise HTTPException(status_code=400, detail="studio_name_required")
        service.repo.update(int(studio_id), **updates)
        updated = service.repo.get(int(studio_id))
        if updated is None:
            raise HTTPException(status_code=404, detail="studio_not_found")
        _enforce_live_mic_ownership(service, int(updated["station_id"]))
        _broadcast_station_state(service, int(updated["station_id"]))
        return {"studio": _studio_row_to_dict(updated)}
    finally:
        conn.close()


@router.post("/api/studios/{studio_id}/join")
def join_studio(studio_id: int, request: Request):
    init_db()
    conn = get_connection()
    try:
        try:
            service = StudioService(conn)
            snapshot = service.join_studio(int(studio_id), _request_user(request))
            _enforce_live_mic_ownership(service, int(snapshot["station_id"]))
            _broadcast_station_state(service, int(snapshot["station_id"]))
            return snapshot
        except Exception as exc:
            _translate_service_error(exc)
    finally:
        conn.close()


@router.post("/api/studios/{studio_id}/leave")
def leave_studio(studio_id: int, request: Request):
    init_db()
    conn = get_connection()
    try:
        try:
            service = StudioService(conn)
            snapshot = service.leave_studio(int(studio_id), _request_user(request))
            _enforce_live_mic_ownership(service, int(snapshot["station_id"]))
            _broadcast_station_state(service, int(snapshot["station_id"]))
            return snapshot
        except Exception as exc:
            _translate_service_error(exc)
    finally:
        conn.close()


@router.post("/api/studios/handoff")
def handoff_studio(payload: StudioHandoffPayload, request: Request):
    init_db()
    conn = get_connection()
    try:
        try:
            service = StudioService(conn)
            snapshot = service.handoff(
                station_id=int(payload.station_id),
                source_studio_id=int(payload.source_studio_id),
                target_studio_id=int(payload.target_studio_id),
                actor=_request_user(request),
            )
            _enforce_live_mic_ownership(service, int(payload.station_id))
            _broadcast_station_state(service, int(payload.station_id))
            return snapshot
        except Exception as exc:
            _translate_service_error(exc)
    finally:
        conn.close()


@router.get("/api/studios/{studio_id}/chat")
def get_studio_chat(studio_id: int, request: Request, limit: int = 50):
    _request_user(request)
    init_db()
    conn = get_connection()
    try:
        service = StudioService(conn)
        row = service.repo.get(int(studio_id))
        if row is None:
            raise HTTPException(status_code=404, detail="studio_not_found")
        return {
            "studio_id": int(studio_id),
            "messages": service.list_chat_messages(int(studio_id), limit=limit),
        }
    finally:
        conn.close()


@router.post("/api/studios/{studio_id}/chat")
def post_studio_chat(studio_id: int, payload: StudioChatPayload, request: Request):
    init_db()
    conn = get_connection()
    try:
        try:
            service = StudioService(conn)
            message = service.send_chat_message(
                int(studio_id),
                _request_user(request),
                str(payload.message or ""),
            )
            studio_row = service.repo.get(int(message["studio_id"]))
            station_id = (
                int(studio_row["station_id"]) if studio_row is not None else int(studio_id)
            )
            broadcaster.on_chat_message(station_id, message)
            return message
        except Exception as exc:
            _translate_service_error(exc)
    finally:
        conn.close()
