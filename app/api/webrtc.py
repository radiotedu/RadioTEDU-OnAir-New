from fastapi import APIRouter

from app.config import get_webrtc_enabled, get_webrtc_ice_servers

router = APIRouter(prefix="/api/webrtc", tags=["webrtc"])


@router.get("/ice-config")
def get_ice_config():
    if not get_webrtc_enabled():
        return {"enabled": False, "ice_servers": []}
    return {"enabled": True, "ice_servers": get_webrtc_ice_servers()}
