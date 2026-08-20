from fastapi.testclient import TestClient

from app.main import app


def test_security_headers_are_added():
    client = TestClient(app)
    response = client.get("/")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["origin-agent-cluster"] == "?1"
    assert response.headers["x-permitted-cross-domain-policies"] == "none"
    assert response.headers["permissions-policy"] == "camera=(), geolocation=(), microphone=(self)"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-cleanroom-public-origin"]


def test_security_headers_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "false")

    client = TestClient(app)
    response = client.get("/")

    assert "x-content-type-options" not in response.headers
    assert "x-frame-options" not in response.headers
    assert "cross-origin-opener-policy" not in response.headers
    assert "cross-origin-resource-policy" not in response.headers
    assert "origin-agent-cluster" not in response.headers
    assert "x-permitted-cross-domain-policies" not in response.headers
    assert "permissions-policy" not in response.headers
    assert "referrer-policy" not in response.headers
    assert response.headers["x-cleanroom-public-origin"]
