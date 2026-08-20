from fastapi.testclient import TestClient

from app.main import app


def test_root_service_worker_route_is_served_from_app_scope():
    client = TestClient(app)

    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "const SHELL_ASSETS" in response.text
