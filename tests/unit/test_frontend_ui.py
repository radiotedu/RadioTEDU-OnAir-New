from fastapi.testclient import TestClient

from app.main import app


def test_root_serves_unified_operator_frontend():
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "RadioTEDU OnAir" in res.text
    assert 'id="loginForm"' in res.text
    assert 'id="operatorNavigation"' in res.text


def test_login_route_serves_unified_sign_in_gate():
    client = TestClient(app)
    res = client.get("/login.html")
    assert res.status_code == 200
    assert 'id="loginForm"' in res.text
    assert "Sign in" in res.text
    assert 'id="appShell"' in res.text


def test_frontend_static_assets_are_served():
    client = TestClient(app)
    res = client.get("/static/onair/app.js")
    assert res.status_code == 200
    assert "async function api(" in res.text
    assert "function initializeOperatorNavigation()" in res.text


def test_frontend_serves_guest_room_audio_client_asset():
    client = TestClient(app)
    res = client.get("/static/onair/guest-room.js")
    assert res.status_code == 200
    assert "MediaRecorder" in res.text


def test_unified_operator_shell_contains_emergency_audio_controls():
    client = TestClient(app)
    res = client.get("/app")
    assert res.status_code == 200
    assert 'id="emergencyLamp"' in res.text
    assert 'id="emergencySignalState"' in res.text
    assert 'id="startEmergencyButton"' in res.text
    assert 'id="stopEmergencyButton"' in res.text
