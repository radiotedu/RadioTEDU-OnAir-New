import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import login_and_get_headers


def test_ws_requires_valid_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?station_id=1"):
            pass


def test_ws_accepts_valid_token_and_replies_to_ping(client):
    token = login_and_get_headers(client, "admin", "changeme")["Authorization"].split(" ", 1)[1]

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        ws.send_json({"type": "ping"})
        message = ws.receive_json()

    assert message["type"] == "pong"
    assert message["station_id"] == 1


def test_ws_receives_studio_events_after_join(client, admin_token_headers, dj_token_headers):
    token = admin_token_headers["Authorization"].split(" ", 1)[1]

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        response = client.post("/api/studios/1/join", headers=dj_token_headers)
        assert response.status_code == 200
        first = ws.receive_json()
        second = ws.receive_json()

    assert {first["type"], second["type"]} == {"studio.status", "dj.presence"}


def test_ws_receives_chat_message_after_chat_post(client, admin_token_headers, dj_token_headers):
    token = admin_token_headers["Authorization"].split(" ", 1)[1]

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        join_response = client.post("/api/studios/1/join", headers=dj_token_headers)
        assert join_response.status_code == 200
        ws.receive_json()
        ws.receive_json()

        post_response = client.post(
            "/api/studios/1/chat",
            headers=dj_token_headers,
            json={"message": "hello websocket"},
        )
        assert post_response.status_code == 200
        message = ws.receive_json()

    assert message["type"] == "chat.message"
    assert message["payload"]["message"] == "hello websocket"


def test_chat_message_event_uses_station_id_not_studio_id(
    client, admin_token_headers, dj_token_headers
):
    token = admin_token_headers["Authorization"].split(" ", 1)[1]
    create_response = client.post(
        "/api/studios",
        headers=admin_token_headers,
        json={"station_id": 1, "name": "Studio B"},
    )
    assert create_response.status_code == 200
    studio_id = int(create_response.json()["studio"]["id"])

    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        join_response = client.post(f"/api/studios/{studio_id}/join", headers=dj_token_headers)
        assert join_response.status_code == 200
        ws.receive_json()
        ws.receive_json()

        post_response = client.post(
            f"/api/studios/{studio_id}/chat",
            headers=dj_token_headers,
            json={"message": "station scoped"},
        )
        assert post_response.status_code == 200
        message = ws.receive_json()

    assert message["type"] == "chat.message"
    assert message["station_id"] == 1
