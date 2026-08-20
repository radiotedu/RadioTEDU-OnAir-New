from pathlib import Path

from tests.conftest import login_and_get_headers


def test_ws_router_source_handles_webrtc_offer():
    router_path = Path(__file__).resolve().parents[2] / "app" / "ws" / "router.py"
    source = router_path.read_text(encoding="utf-8")
    assert "webrtc.offer" in source
    assert "webrtc.close" in source
    assert "webrtc.ice" in source


def test_webrtc_offer_rejected_when_disabled(client, monkeypatch):
    monkeypatch.setenv("WEBRTC_ENABLED", "false")
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    token = str(admin_headers["Authorization"]).split(" ", 1)[1]
    join = client.post("/api/studios/1/join", headers=admin_headers)
    assert join.status_code == 200

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "webrtc.offer", "sdp": "v=0\r\n"})
        msg = ws.receive_json()

    assert msg["type"] == "webrtc.error"
    assert "disabled" in msg["payload"]["detail"]


def test_webrtc_offer_rejected_for_producer(client):
    from tests.conftest import login_and_get_headers as _login
    # Ensure producer user exists
    from tests.conftest import _ensure_user
    _ensure_user("producer", "Producer", "changeme", "producer")
    producer_headers = _login(client, "producer", "changeme")
    token = str(producer_headers["Authorization"]).split(" ", 1)[1]

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "webrtc.offer", "sdp": "v=0\r\n"})
        msg = ws.receive_json()

    assert msg["type"] in ("error", "webrtc.error")


def test_webrtc_close_does_not_crash_without_session(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    token = str(admin_headers["Authorization"]).split(" ", 1)[1]

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "webrtc.close"})
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()

    assert msg["type"] == "pong"
