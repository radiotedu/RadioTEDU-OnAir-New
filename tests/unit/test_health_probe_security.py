from fastapi.testclient import TestClient

from app.main import app


def test_unauthenticated_probes_are_minimal_and_detailed_health_is_protected():
    client = TestClient(app)
    no_auth = {"X-Test-No-Auto-Auth": "1"}

    detailed = client.get("/api/health", headers=no_auth)
    live = client.get("/api/health/live", headers=no_auth)
    ready = client.get("/api/health/ready", headers=no_auth)

    assert detailed.status_code == 401
    assert live.status_code == 200
    assert ready.status_code in {200, 503}
    assert set(live.json()) == {
        "backend_instance_id",
        "backend_process_id",
        "service",
        "state",
        "status",
        "version",
    }
    assert set(ready.json()) == {
        "backend_instance_id",
        "backend_process_id",
        "broadcast_safe",
        "database",
        "ready",
        "service",
        "state",
        "status",
        "version",
    }
    serialized = ready.text.lower()
    assert "cleanroom.db" not in serialized
    assert "traceback" not in serialized
    assert "exception" not in serialized
