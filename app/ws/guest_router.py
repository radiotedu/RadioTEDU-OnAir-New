from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.audio.guest_audio_registry import GuestReturnAudioTrack, guest_audio_registry
from app.audio.rtc_mic_session import RtcMicSession, aiortc_available
from app.config import get_webrtc_enabled, get_webrtc_ice_servers
from app.services.guest_room_service import GuestRoomError, guest_room_service

router = APIRouter()
logger = logging.getLogger("cleanroom.guest-webrtc")


@router.websocket("/ws/guest")
async def guest_websocket(websocket: WebSocket):
    await websocket.accept()
    session = None
    rtc = None
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if str(first.get("type") or "") != "guest.auth":
            await websocket.send_json({"type": "guest.error", "detail": "authentication_required"})
            await websocket.close(code=4401)
            return
        session = guest_room_service.authenticate_session(str(first.get("session_token") or ""))
        guest_room_service.set_connected(int(session["id"]), True, "connecting")
        await websocket.send_json(
            {
                "type": "guest.authenticated",
                "session": {
                    "id": int(session["id"]),
                    "studio_id": int(session["studio_id"]),
                    "station_id": int(session["station_id"]),
                    "display_name": str(session["display_name"]),
                    "status": str(session["status"]),
                },
                "ice_servers": get_webrtc_ice_servers() if get_webrtc_enabled() else [],
            }
        )
        while True:
            message = await websocket.receive_json()
            event_type = str(message.get("type") or "")
            if event_type == "ping":
                guest_room_service.set_connected(int(session["id"]), True, "connected")
                await websocket.send_json({"type": "pong"})
                continue
            if event_type == "guest.mute":
                result = guest_room_service.guest_self_mute(str(first.get("session_token") or ""), bool(message.get("muted")))
                await websocket.send_json({"type": "guest.mute.updated", **result})
                continue
            if event_type == "webrtc.offer":
                if not get_webrtc_enabled() or not aiortc_available():
                    await websocket.send_json({"type": "webrtc.error", "detail": "webrtc_disabled"})
                    continue
                if rtc is not None:
                    await rtc.stop()
                rtc = RtcMicSession(
                    station_id=int(session["station_id"]),
                    return_track=GuestReturnAudioTrack(int(session["id"])),
                )
                answer = await rtc.set_offer(str(message.get("sdp") or ""), ice_servers=get_webrtc_ice_servers())
                guest_audio_registry.register(int(session["id"]), rtc)
                guest_room_service.set_connected(int(session["id"]), True, "connected")
                await websocket.send_json({"type": "webrtc.answer", "sdp": answer})
                continue
            if event_type == "webrtc.ice" and rtc is not None:
                await rtc.add_ice_candidate(dict(message.get("candidate") or {}))
                continue
            if event_type == "webrtc.close":
                break
    except (asyncio.TimeoutError, GuestRoomError):
        try:
            await websocket.send_json({"type": "guest.error", "detail": "invalid_guest_session"})
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Guest websocket failed: %s", exc)
        try:
            await websocket.send_json({"type": "guest.error", "detail": "guest_connection_failed"})
        except Exception:
            pass
    finally:
        if session is not None:
            guest_room_service.set_connected(int(session["id"]), False, "disconnected")
            registered = guest_audio_registry.unregister(int(session["id"]))
            target = registered or rtc
            if target is not None:
                try:
                    await target.stop()
                except Exception:
                    pass
        try:
            await websocket.close()
        except Exception:
            pass
