from fastapi.testclient import TestClient

from app.main import app


def test_program_music_mode_and_queue_source():
    c = TestClient(app)
    r = c.post("/api/liquidsoap/program/music", params={"mode": "duck", "station_id": 1})
    assert r.status_code == 200
    assert r.json()["effective_mode"] in {"duck", "normal", "mute"}


def test_program_queue_source_is_station_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)
    stations = c.get("/api/stations")
    assert stations.status_code == 200
    created = c.post("/api/stations", json={"name": "Station Two"})
    assert created.status_code == 200

    src = c.post("/api/program/queue/source", json={"station_id": 1, "source": "host"})
    assert src.status_code == 200

    station_one = c.get("/api/program/queue", params={"station_id": 1})
    station_two = c.get("/api/program/queue", params={"station_id": 2})
    assert station_one.status_code == 200
    assert station_two.status_code == 200
    assert station_one.json()["source"] == "host"
    assert station_two.json()["source"] == "automation"


def test_program_queue_contract_matches_frontend_expectations(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    created_ids = []
    for i in range(1, 4):
        tr = c.post(
            "/api/tracks",
            json={
                "station_id": 1,
                "title": f"Q{i}",
                "artist": "Host",
                "track_type": "music",
                "duration": 180,
                "file_path": f"C:/program/q{i}.mp3",
            },
        )
        assert tr.status_code == 200
        created_ids.append(int(tr.json()["track_id"]))

    src = c.post("/api/program/queue/source", json={"station_id": 1, "source": "host"})
    assert src.status_code == 200
    src_data = src.json()
    assert src_data["source"] == "host"
    assert src_data["effective_source"] == "automation"
    assert src_data["queue"]["fallback_active"] is True
    assert int(src_data["queue"]["host_min_tracks_to_activate"]) >= 1

    for tid in created_ids:
        add = c.post("/api/program/queue/items", json={"station_id": 1, "track_id": tid})
        assert add.status_code == 200
        q = add.json().get("queue") or {}
        assert isinstance(q.get("items"), list)
        assert q["source"] == "host"
        assert "effective_source" in q
        assert "fallback_active" in q
        assert "host_min_tracks_to_activate" in q
        if q["items"]:
            assert "file_path" in q["items"][0]
            assert "duration" in q["items"][0]

    state = c.get("/api/program/queue", params={"station_id": 1})
    assert state.status_code == 200
    state_data = state.json()
    assert state_data["source"] == "host"
    assert state_data["effective_source"] == "host"
    assert state_data["fallback_active"] is False
    assert len(state_data["items"]) == 3

    moved = c.post(
        "/api/program/queue/move",
        json={"station_id": 1, "from_index": 2, "to_index": 0},
    )
    assert moved.status_code == 200
    moved_items = (moved.json().get("queue") or {}).get("items") or []
    assert len(moved_items) == 3
    assert int(moved_items[0]["track_id"]) == int(created_ids[2])

    deleted = c.delete("/api/program/queue/1", params={"station_id": 1})
    assert deleted.status_code == 200
    after_delete = (deleted.json().get("queue") or {}).get("items") or []
    assert len(after_delete) == 2
