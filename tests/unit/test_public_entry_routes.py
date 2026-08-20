from fastapi.testclient import TestClient

from app.main import app


def test_public_root_serves_unified_sign_in_shell():
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "RadioTEDU OnAir sign in" in res.text
    assert 'id="loginForm"' in res.text
    assert 'id="operatorNavigation"' in res.text


def test_app_route_serves_authenticated_operator_shell():
    client = TestClient(app)
    res = client.get("/app")
    assert res.status_code == 200
    assert "RadioTEDU OnAir" in res.text
    assert 'id="stationSelect"' in res.text
    assert 'id="startBroadcastButton"' in res.text


def test_login_route_serves_same_unified_shell():
    client = TestClient(app)
    res = client.get("/login.html")
    assert res.status_code == 200
    assert 'id="loginForm"' in res.text
    assert "Sign in" in res.text
    assert 'id="operatorNavigation"' in res.text
