from app import config
from tests.conftest import _ensure_user, login_and_get_headers


def test_ice_config_requires_auth(client):
    response = client.get(
        "/api/webrtc/ice-config",
        headers={"X-Test-No-Auto-Auth": "1"},
    )
    assert response.status_code == 401


def test_ice_config_returns_stun_by_default(client, monkeypatch):
    monkeypatch.setattr(config, "_webrtc_runtime_available", lambda: True)
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    response = client.get("/api/webrtc/ice-config", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert "ice_servers" in body
    assert len(body["ice_servers"]) >= 1
    assert "stun:" in body["ice_servers"][0]["urls"]


def test_ice_config_includes_turn_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "_webrtc_runtime_available", lambda: True)
    monkeypatch.setenv("WEBRTC_TURN_URL", "turn:t.example.com:3478")
    monkeypatch.setenv("WEBRTC_TURN_USERNAME", "u")
    monkeypatch.setenv("WEBRTC_TURN_CREDENTIAL", "p")
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    response = client.get("/api/webrtc/ice-config", headers=admin_headers)
    body = response.json()
    assert len(body["ice_servers"]) == 2
    turn = body["ice_servers"][1]
    assert turn["urls"] == "turn:t.example.com:3478"
    assert turn["username"] == "u"
    assert turn["credential"] == "p"


def test_ice_config_disabled_when_webrtc_off(client, monkeypatch):
    monkeypatch.setenv("WEBRTC_ENABLED", "false")
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    response = client.get("/api/webrtc/ice-config", headers=admin_headers)
    body = response.json()
    assert body["enabled"] is False


def test_ice_config_forbidden_for_viewer(client):
    _ensure_user("viewer", "Viewer User", "changeme", "viewer")
    viewer_headers = login_and_get_headers(client, "viewer", "changeme")
    response = client.get("/api/webrtc/ice-config", headers=viewer_headers)
    assert response.status_code == 403
