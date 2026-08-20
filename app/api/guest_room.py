from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user, user_has_any_show_permission, user_has_permission
from app.runtime_paths import get_data_dir
from app.audio.guest_audio_registry import guest_talkback_registry
from app.audio.live_mic_registry import live_mic_registry
from app.services.guest_room_service import GuestRoomError, guest_room_service
from app.services.program_recording import ProgramRecordingError, program_recording_service

router = APIRouter()


class GuestRedeemPayload(BaseModel):
    invite_token: str
    display_name: str = Field(min_length=1, max_length=80)


class GuestSessionPayload(BaseModel):
    session_token: str


class GuestMutePayload(GuestSessionPayload):
    muted: bool


class GuestConsentPayload(GuestSessionPayload):
    recording_id: int
    accepted: bool


class GuestAudioPayload(BaseModel):
    muted: bool | None = None
    on_air: bool | None = None
    gain_db: float | None = Field(default=None, ge=-24, le=12)


class TalkbackStartPayload(BaseModel):
    station_id: int
    input_format: str = "webm"


async def _guest_manager(request: Request):
    user = await get_current_user(request)
    if str(user.get("role") or "") == "admin" or user_has_permission(user, "shows.manage") or user_has_any_show_permission(user, "show.guest_manage"):
        return user
    raise HTTPException(status_code=403, detail="guest_manage_forbidden")


async def _guest_recorder(request: Request):
    user = await get_current_user(request)
    if str(user.get("role") or "") == "admin" or user_has_permission(user, "shows.manage") or user_has_any_show_permission(user, "show.guest_record"):
        return user
    raise HTTPException(status_code=403, detail="guest_record_forbidden")


def _translate(exc: Exception):
    detail = str(exc) or "guest_room_failed"
    if detail.endswith("not_found") or detail == "studio_not_found":
        status = 404
    elif any(token in detail for token in ("full", "consent", "already", "active", "not_in_lobby", "not_admitted", "unavailable")):
        status = 409
    elif "invalid" in detail or "expired" in detail or "required" in detail:
        status = 400
    else:
        status = 400
    raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/api/guest/redeem")
def redeem_guest_invite(payload: GuestRedeemPayload):
    try:
        return guest_room_service.redeem(payload.invite_token, payload.display_name)
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/guest/session")
def get_guest_session(payload: GuestSessionPayload):
    try:
        session = guest_room_service.authenticate_session(payload.session_token)
        result = {key: session[key] for key in ("id", "studio_id", "station_id", "display_name", "status", "is_connected", "is_muted", "is_on_air", "connection_quality")}
        from app.db import get_connection
        conn = get_connection()
        try:
            consent = conn.execute(
                "SELECT c.recording_id, c.decision, r.status FROM guest_recording_consents c "
                "JOIN guest_recordings r ON r.id=c.recording_id "
                "WHERE c.session_id=? AND r.status IN ('pending_consent','recording') ORDER BY c.recording_id DESC LIMIT 1",
                (int(session["id"]),),
            ).fetchone()
            result["recording_consent"] = dict(consent) if consent else None
        finally:
            conn.close()
        return result
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/guest/mute")
def guest_self_mute(payload: GuestMutePayload):
    try:
        return guest_room_service.guest_self_mute(payload.session_token, payload.muted)
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/guest/consent")
def guest_recording_consent(payload: GuestConsentPayload):
    try:
        return program_recording_service.consent(payload.recording_id, payload.session_token, payload.accepted)
    except (GuestRoomError, ProgramRecordingError) as exc:
        _translate(exc)


@router.get("/api/studios/{studio_id}/guest-room")
def guest_room_snapshot(studio_id: int, _user=Depends(_guest_manager)):
    try:
        return guest_room_service.snapshot(studio_id)
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-invites")
def create_guest_invite(studio_id: int, request: Request, user=Depends(_guest_manager)):
    try:
        return guest_room_service.create_invite(studio_id, actor_id=int(user["id"]), base_url=str(request.base_url).rstrip("/"))
    except GuestRoomError as exc:
        _translate(exc)


@router.delete("/api/studios/{studio_id}/guest-invites/{invite_id}")
def revoke_guest_invite(studio_id: int, invite_id: int, user=Depends(_guest_manager)):
    try:
        guest_room_service.revoke_invite(studio_id, invite_id, actor_id=int(user["id"]))
        return {"ok": True}
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/{session_id}/admit")
def admit_guest(studio_id: int, session_id: int, user=Depends(_guest_manager)):
    try:
        return guest_room_service.admit(studio_id, session_id, actor_id=int(user["id"]))
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/{session_id}/reject")
def reject_guest(studio_id: int, session_id: int, user=Depends(_guest_manager)):
    try:
        return guest_room_service.reject_or_kick(studio_id, session_id, actor_id=int(user["id"]), action="reject")
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/{session_id}/kick")
def kick_guest(studio_id: int, session_id: int, user=Depends(_guest_manager)):
    try:
        return guest_room_service.reject_or_kick(studio_id, session_id, actor_id=int(user["id"]), action="kick")
    except GuestRoomError as exc:
        _translate(exc)


@router.patch("/api/studios/{studio_id}/guest-room/{session_id}/audio")
def update_guest_audio(studio_id: int, session_id: int, payload: GuestAudioPayload, user=Depends(_guest_manager)):
    try:
        updated = guest_room_service.update_audio(studio_id, session_id, muted=payload.muted, on_air=payload.on_air, gain_db=payload.gain_db, actor_id=int(user["id"]))
        if payload.on_air:
            try:
                from app.api.runtime import runtime_registry
                runtime_registry.promote_live_mix(int(updated["station_id"]))
            except Exception:
                pass
        return updated
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/all-off-air")
def all_guests_off_air(studio_id: int, user=Depends(_guest_manager)):
    try:
        return guest_room_service.all_off_air(studio_id, actor_id=int(user["id"]))
    except GuestRoomError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/talkback/start")
def start_guest_talkback(studio_id: int, payload: TalkbackStartPayload, _user=Depends(_guest_manager)):
    room = guest_room_service.snapshot(studio_id)
    if int(room["station_id"]) != int(payload.station_id):
        raise HTTPException(status_code=400, detail="station_mismatch")
    mic = live_mic_registry.snapshot(int(payload.station_id))
    if bool(mic.get("transmitting")):
        raise HTTPException(status_code=409, detail="talkback_requires_host_mic_off_air")
    try:
        return guest_talkback_registry.start(payload.station_id, payload.input_format)
    except RuntimeError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/talkback/chunk")
async def push_guest_talkback(studio_id: int, request: Request, station_id: int, _user=Depends(_guest_manager)):
    room = guest_room_service.snapshot(studio_id)
    if int(room["station_id"]) != int(station_id):
        raise HTTPException(status_code=400, detail="station_mismatch")
    try:
        return guest_talkback_registry.push(station_id, await request.body())
    except RuntimeError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-room/talkback/stop")
def stop_guest_talkback(studio_id: int, station_id: int, _user=Depends(_guest_manager)):
    room = guest_room_service.snapshot(studio_id)
    if int(room["station_id"]) != int(station_id):
        raise HTTPException(status_code=400, detail="station_mismatch")
    return guest_talkback_registry.stop(station_id)


@router.post("/api/studios/{studio_id}/guest-recordings")
def request_guest_recording(studio_id: int, user=Depends(_guest_recorder)):
    try:
        return program_recording_service.request(studio_id, actor_id=int(user["id"]))
    except ProgramRecordingError as exc:
        _translate(exc)


@router.post("/api/studios/{studio_id}/guest-recordings/{recording_id}/stop")
def stop_guest_recording(studio_id: int, recording_id: int, _user=Depends(_guest_recorder)):
    try:
        status = program_recording_service.status(recording_id)
        if int(status["studio_id"]) != int(studio_id):
            raise ProgramRecordingError("recording_not_found")
        return program_recording_service.stop(recording_id)
    except ProgramRecordingError as exc:
        _translate(exc)


@router.get("/api/guest-recordings")
def list_guest_recordings(station_id: int, _user=Depends(_guest_recorder)):
    return {"recordings": program_recording_service.list(station_id)}


@router.get("/api/guest-recordings/{recording_id}/download")
def download_guest_recording(recording_id: int, _user=Depends(_guest_recorder)):
    try:
        recording = program_recording_service.status(recording_id)
    except ProgramRecordingError as exc:
        _translate(exc)
    path = Path(str(recording.get("file_path") or "")).resolve()
    root = get_data_dir().resolve()
    if not path.is_file() or root not in path.parents:
        raise HTTPException(status_code=404, detail="recording_file_not_found")
    return FileResponse(path, media_type="audio/flac", filename=path.name)


@router.delete("/api/guest-recordings/{recording_id}")
def delete_guest_recording(recording_id: int, user=Depends(_guest_recorder)):
    try:
        return program_recording_service.delete(recording_id, actor_id=int(user["id"]))
    except ProgramRecordingError as exc:
        _translate(exc)
