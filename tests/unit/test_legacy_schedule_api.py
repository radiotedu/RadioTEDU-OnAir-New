from fastapi.testclient import TestClient

from app.main import app


def test_legacy_schedule_and_timeline():
    c = TestClient(app)
    created = c.post(
        "/api/schedule",
        json={
            "station_id": 1,
            "playlist_id": 33,
            "event_name": "Noon Mix",
            "start_time": "12:00",
            "end_time": "13:00",
            "day_of_week": "*",
        },
    )
    assert created.status_code == 200
    schedule = c.get("/api/schedule", params={"station_id": 1})
    assert schedule.status_code == 200
    items = schedule.json()
    assert isinstance(items, list)
    assert any("start_time" in item and "end_time" in item for item in items)
    timeline = c.get("/api/schedule/timeline", params={"station_id": 1})
    assert timeline.status_code == 200
    payload = timeline.json()
    assert "items" in payload
    assert isinstance(payload.get("blocks"), list)


def test_schedule_delete_rejects_wrong_station_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)
    stations = c.get("/api/stations")
    assert stations.status_code == 200
    created_station = c.post("/api/stations", json={"name": "Station Two"})
    assert created_station.status_code == 200

    created = c.post(
        "/api/schedule",
        json={
            "station_id": 2,
            "playlist_id": 44,
            "event_name": "Station Two Block",
            "start_time": "18:00",
            "end_time": "19:00",
            "day_of_week": "*",
        },
    )
    assert created.status_code == 200
    schedule_id = int(created.json()["id"])

    wrong_scope = c.delete(f"/api/schedule/{schedule_id}", params={"station_id": 1})
    assert wrong_scope.status_code == 404

    listed = c.get("/api/schedule", params={"station_id": 2})
    assert listed.status_code == 200
    assert any(int(item["id"]) == schedule_id for item in listed.json())
