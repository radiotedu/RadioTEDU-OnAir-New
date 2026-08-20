from fastapi.testclient import TestClient

from app.main import app


def test_request_id_header_is_added_for_api_response():
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
    request_id = str(res.headers.get("x-request-id", "")).strip()
    assert request_id != ""


def test_request_id_header_echoes_incoming_value():
    client = TestClient(app)
    res = client.get("/api/health", headers={"X-Request-ID": "req-123"})
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "req-123"
