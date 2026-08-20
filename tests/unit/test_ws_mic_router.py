from pathlib import Path

from app.services.studio_service import StudioService
from app.ws.broadcaster import broadcaster
from tests.conftest import login_and_get_headers


def _access_token(headers: dict[str, str]) -> str:
    return str(headers["Authorization"]).split(" ", 1)[1]


def test_ws_mic_start_returns_live_status_for_admin(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    token = _access_token(admin_headers)

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "mic.start", "station_id": 1})
        message = ws.receive_json()

    assert message["type"] == "mic.status"
    assert message["station_id"] == 1
    assert message["payload"]["transmitting"] is True


def test_ws_admin_binary_mic_frames_do_not_require_studio_claim(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    token = _access_token(admin_headers)

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "mic.start", "station_id": 1})
        started = ws.receive_json()
        ws.send_bytes(b"\x00\x01")
        frame_result = ws.receive_json()

    assert started["type"] == "mic.status"
    assert frame_result["type"] == "mic.status"
    assert frame_result["payload"]["transmitting"] is True


def test_ws_mic_start_delegates_through_service_boundary(
    client, monkeypatch
):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    token = _access_token(admin_headers)
    join_response = client.post("/api/studios/1/join", headers=admin_headers)
    assert join_response.status_code == 200

    called = {}

    def fake_start_mic_transmission(self, station_id: int, actor: dict):
        called["station_id"] = int(station_id)
        called["actor"] = dict(actor)
        payload = {
            "station_id": int(station_id),
            "transmitting": True,
            "active_user": dict(actor),
        }
        broadcaster.on_mic_status(int(station_id), payload)
        return payload

    def fail_start_transmission(*args, **kwargs):
        raise AssertionError("router should not call live_mic_registry directly")

    monkeypatch.setattr(
        "app.ws.router.live_mic_registry.start_transmission",
        fail_start_transmission,
    )
    monkeypatch.setattr(
        StudioService,
        "start_mic_transmission",
        fake_start_mic_transmission,
        raising=False,
    )

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "mic.start", "station_id": 1})
        message = ws.receive_json()

    assert message["type"] == "mic.status"
    assert message["station_id"] == 1
    assert message["payload"]["transmitting"] is True
    assert called["station_id"] == 1
    assert called["actor"]["username"] == "admin"


def test_ws_mic_start_forbidden_for_producer(client, producer_token_headers):
    token = _access_token(producer_token_headers)

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "mic.start", "station_id": 1})
        message = ws.receive_json()

    assert message["type"] == "error"
    assert message["payload"]["detail"] in ("forbidden", "not_studio_owner", "no_dj_session")


def test_ws_mic_start_forbidden_for_dj_without_on_air_studio_ownership(
    client, dj_token_headers
):
    token = _access_token(dj_token_headers)

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "mic.start", "station_id": 1})
        message = ws.receive_json()

    assert message["type"] == "error"
    assert message["payload"]["detail"] == "not_studio_owner"


def test_ws_binary_mic_frames_require_on_air_studio_ownership(client, dj_token_headers):
    token = _access_token(dj_token_headers)

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_bytes(b"\x00\x01")
        message = ws.receive_json()

    assert message["type"] == "mic.error"
    assert message["payload"]["detail"] == "not_studio_owner"


def test_ws_router_source_handles_raw_messages_for_binary_mic_frames():
    router_path = Path(__file__).resolve().parents[2] / "app" / "ws" / "router.py"
    source = router_path.read_text(encoding="utf-8")

    assert "await websocket.receive()" in source
    assert "bytes" in source or "receive_bytes" in source
