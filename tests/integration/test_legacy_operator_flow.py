from fastapi.testclient import TestClient

from app.main import app


def test_legacy_operator_flow_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)
    created_station = c.post("/api/stations", json={"name": "Main"})
    assert created_station.status_code == 200

    created_track = c.post(
        "/api/tracks",
        json={"title": "Song", "artist": "Artist", "file_path": "C:/x.mp3"},
    )
    assert created_track.status_code == 200

    pushed = c.post("/api/queue/push", json={"station_id": 1, "track_id": 1})
    assert pushed.status_code == 200

    queue = c.get("/api/queue", params={"station_id": 1})
    assert queue.status_code == 200
    payload = queue.json()
    assert isinstance(payload.get("items"), list)
    assert len(payload["items"]) >= 1
    assert int(payload["items"][0]["station_id"]) == 1

    mode = c.post("/api/liquidsoap/program/music", params={"mode": "duck", "station_id": 1})
    assert mode.status_code == 200
    assert mode.json()["effective_mode"] == "duck"

    program_source = c.post("/api/program/queue/source", json={"station_id": 1, "source": "host"})
    assert program_source.status_code == 200
    station_two = c.post("/api/stations", json={"name": "Backup"})
    assert station_two.status_code == 200
    station_two_queue = c.get("/api/program/queue", params={"station_id": 2})
    assert station_two_queue.status_code == 200
    station_two_payload = station_two_queue.json()
    assert station_two_payload["station_id"] == 2
    assert station_two_payload["source"] == "automation"
    assert station_two_payload["queue_total"] == 0

    logs = c.get("/api/logs", params={"station_id": 1})
    assert logs.status_code == 200
    assert isinstance(logs.json(), list)
