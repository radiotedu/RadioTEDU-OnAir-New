from fastapi.testclient import TestClient

from app.main import app


def test_public_origin_defaults_to_request_origin(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["x-cleanroom-public-origin"] == "http://testserver"


def test_public_origin_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://radio.example.com")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["x-cleanroom-public-origin"] == "https://radio.example.com"


def test_public_origin_uses_forwarded_headers_when_enabled(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    client = TestClient(app)
    response = client.get(
        "/",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "radio.example.com",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-cleanroom-public-origin"] == "https://radio.example.com"
