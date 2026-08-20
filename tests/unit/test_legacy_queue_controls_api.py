from fastapi.testclient import TestClient

from app.main import app


def test_legacy_queue_controls():
    c = TestClient(app)
    c.post("/api/queue/push", json={"station_id": 1, "track_id": 77})
    listed = c.get("/api/queue", params={"station_id": 1})
    assert listed.status_code == 200
    payload = listed.json()
    assert isinstance(payload.get("items"), list)
    assert int(payload.get("station_id", 0)) == 1
