import json
import logging
import uuid

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.audio.live_mic_registry import live_mic_registry
from app.audio.rtc_mic_session import RtcMicSession, aiortc_available
from app.auth.jwt_handler import decode_token
from app.config import get_webrtc_enabled, get_webrtc_ice_servers
from app.db import get_connection, init_db
from app.services.studio_service import StudioForbiddenError, StudioService
from app.repositories.user_repo import UserRepository
from app.ws.broadcaster import EventBroadcaster, broadcaster, connection_manager
from app.ws.events import (
    EVENT_MIC_ERROR,
    EVENT_MIC_STATUS,
    EVENT_PONG,
    EVENT_WEBRTC_ANSWER,
    EVENT_WEBRTC_ERROR,
    EVENT_WEBRTC_ICE,
    make_event,
)

_logger = logging.getLogger("cleanroom.webrtc")

router = APIRouter()


def _load_user_from_access_token(token: str):
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if str(payload.get("type") or "") != "access":
        return None

    init_db()
    conn = get_connection()
    try:
        user = UserRepository(conn).get_user_by_id(int(payload["sub"]))
        if user is None or int(user["is_active"]) != 1:
            return None
        return user
    finally:
        conn.close()


def _can_user_activate_mic(station_id: int, user: dict) -> tuple[bool, str]:
    role = str((user or {}).get("role") or "").strip().lower()
    if role in {"admin", "superadmin"}:
        return True, "ok"
    init_db()
    conn = get_connection()
    try:
        return StudioService(conn).can_user_activate_mic(
            int(station_id), int(user["id"])
        )
    finally:
        conn.close()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = None,
    station_id: int = 1,
):
    user = _load_user_from_access_token(str(token or ""))
    if user is None:
        await websocket.close(code=4401)
        return

    sid = max(1, int(station_id))
    connection_id = uuid.uuid4().hex
    rtc_station_id = None  # tracks which station this connection has an RTC session for
    user_room = f"user:{int(user['id'])}"
    user_payload = {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
    }
    await connection_manager.connect(
        websocket,
        connection_id=connection_id,
        rooms={EventBroadcaster.station_room(sid), "all", user_room},
        user={"id": int(user["id"]), "username": str(user["username"])},
    )

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect
            if "bytes" in message and message.get("bytes") is not None:
                allowed, detail = _can_user_activate_mic(sid, user_payload)
                if not allowed:
                    await websocket.send_json(
                        make_event(
                            EVENT_MIC_ERROR,
                            sid,
                            {"detail": detail},
                        )
                    )
                    continue
                try:
                    status_payload = live_mic_registry.push_chunk(sid, message["bytes"])
                    await broadcaster.broadcast_mic_status(sid, status_payload)
                except Exception as exc:
                    await websocket.send_json(
                        make_event(
                            EVENT_MIC_ERROR,
                            sid,
                            {"detail": str(exc) or "mic_chunk_error"},
                        )
                    )
                continue

            raw_text = str(message.get("text") or "").strip()
            if not raw_text:
                await websocket.send_json(
                    make_event(
                        "error",
                        sid,
                        {"detail": "unsupported_message"},
                    )
                )
                continue
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                await websocket.send_json(
                    make_event(
                        "error",
                        sid,
                        {"detail": "invalid_json"},
                    )
                )
                continue
            event_type = str((payload or {}).get("type") or "").strip().lower()
            if event_type == "ping":
                await websocket.send_json(
                    make_event(
                        EVENT_PONG,
                        sid,
                        {"connection_id": connection_id},
                    )
                )
                continue
            if event_type == "subscribe":
                next_station_id = max(1, int((payload or {}).get("station_id") or sid))
                sid = next_station_id
                await connection_manager.move_to_rooms(
                    connection_id,
                    {EventBroadcaster.station_room(sid), "all", user_room},
                )
                await websocket.send_json(
                    make_event(
                        "subscribed",
                        sid,
                        {"rooms": sorted(connection_manager.rooms_for(connection_id))},
                    )
                )
                continue
            if event_type == "mic.start":
                init_db()
                conn = get_connection()
                try:
                    StudioService(conn).start_mic_transmission(sid, user_payload)
                except StudioForbiddenError as exc:
                    await websocket.send_json(
                        make_event(
                            "error",
                            sid,
                            {"detail": str(exc) or "forbidden"},
                        )
                    )
                    continue
                finally:
                    conn.close()
                continue
            if event_type == "mic.stop":
                live_mic_registry.stop_transmission(
                    sid, user_id=int(user["id"])
                )
                continue
            if event_type == "webrtc.offer":
                if not get_webrtc_enabled() or not aiortc_available():
                    await websocket.send_json(
                        make_event(EVENT_WEBRTC_ERROR, sid, {"detail": "webrtc_disabled"})
                    )
                    continue
                allowed, detail = _can_user_activate_mic(sid, user_payload)
                if not allowed:
                    await websocket.send_json(
                        make_event(EVENT_WEBRTC_ERROR, sid, {"detail": detail})
                    )
                    continue
                offer_sdp = str((payload or {}).get("sdp") or "").strip()
                if not offer_sdp:
                    await websocket.send_json(
                        make_event(EVENT_WEBRTC_ERROR, sid, {"detail": "missing_sdp"})
                    )
                    continue
                try:
                    # Delegate through StudioService for ownership/business logic
                    init_db()
                    conn = get_connection()
                    try:
                        StudioService(conn).start_mic_transmission(sid, user_payload)
                    except StudioForbiddenError as exc:
                        await websocket.send_json(
                            make_event(EVENT_WEBRTC_ERROR, sid, {"detail": str(exc) or "forbidden"})
                        )
                        continue
                    finally:
                        conn.close()
                    rtc_session = RtcMicSession(station_id=sid)
                    answer_sdp = await rtc_session.set_offer(offer_sdp, ice_servers=get_webrtc_ice_servers())
                    live_mic_registry.start_rtc_session(sid, rtc_session, user_payload)
                    rtc_station_id = sid
                    await websocket.send_json(
                        make_event(EVENT_WEBRTC_ANSWER, sid, {"sdp": answer_sdp})
                    )
                    await broadcaster.broadcast_mic_status(
                        sid, live_mic_registry.snapshot(sid)
                    )
                except Exception as exc:
                    _logger.exception("WebRTC offer handling failed for station %d", sid)
                    await websocket.send_json(
                        make_event(EVENT_WEBRTC_ERROR, sid, {"detail": str(exc) or "offer_failed"})
                    )
                continue

            if event_type == "webrtc.ice":
                candidate = (payload or {}).get("candidate")
                if candidate and rtc_station_id is not None:
                    rtc = live_mic_registry.get_rtc_session(rtc_station_id)
                    if rtc is not None:
                        try:
                            await rtc.add_ice_candidate(candidate)
                        except Exception as exc:
                            _logger.warning("ICE candidate error: %s", exc)
                continue

            if event_type == "webrtc.close":
                if rtc_station_id is not None:
                    live_mic_registry.stop_rtc_session(rtc_station_id, user_id=int(user["id"]))
                    await broadcaster.broadcast_mic_status(
                        rtc_station_id, live_mic_registry.snapshot(rtc_station_id)
                    )
                    rtc_station_id = None
                continue

            await websocket.send_json(
                make_event(
                    "error",
                    sid,
                    {"detail": "unsupported_message"},
                )
            )
    except WebSocketDisconnect:
        connection_manager.disconnect(connection_id)
    except Exception:
        connection_manager.disconnect(connection_id)
        raise
    finally:
        if rtc_station_id is not None:
            live_mic_registry.stop_rtc_session(rtc_station_id, user_id=int(user["id"]))
        connection_manager.disconnect(connection_id)
