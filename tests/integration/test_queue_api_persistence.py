from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def test_queue_push_writes_queue_and_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)
    res = client.post("/api/queue/push", json={"station_id": 1, "track_id": 77})
    assert res.status_code == 200
    assert res.json()["deduped"] is False

    res2 = client.post("/api/queue/push", json={"station_id": 1, "track_id": 77})
    assert res2.status_code == 200
    assert res2.json()["deduped"] is True

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM queue_items WHERE station_id=1")
    assert cur.fetchone()[0] == 1
    cur.execute(
        "SELECT COUNT(*) FROM command_outbox WHERE station_id=1 AND command_type='queue_push'"
    )
    assert cur.fetchone()[0] == 1
    conn.close()


def test_operator_queue_mutations_return_persisted_runtime_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    first = client.post("/api/queue/push", json={"station_id": 1, "track_id": 101})
    second = client.post("/api/queue/push", json={"station_id": 1, "track_id": 102})
    assert first.status_code == 200
    assert second.status_code == 200
    for response in (first, second):
        acknowledgement = response.json()["runtime_acknowledgement"]
        assert acknowledgement["persisted"] is True
        assert acknowledgement["current_track_interrupted"] is False
        assert isinstance(acknowledgement["queue_event_published"], bool)
        assert isinstance(acknowledgement["worker_running"], bool)

    snapshot = client.get("/api/queue?station_id=1").json()
    assert len(snapshot["revision"]) >= 8
    first_item = next(item for item in snapshot["items"] if item["track_id"] == 101)
    second_item = next(item for item in snapshot["items"] if item["track_id"] == 102)
    moved = client.post(
        "/api/queue/move",
        json={
            "station_id": 1,
            "item_id": first_item["id"],
            "to_index": second_item["queue_index"],
            "expected_revision": snapshot["revision"],
        },
    )
    assert moved.status_code == 200
    assert moved.json()["runtime_acknowledgement"]["persisted"] is True
    assert moved.json()["worker_acknowledgement"]["observed"] is False
    assert [item["track_id"] for item in moved.json()["queue"]["items"]] == [102, 101]

    moved_snapshot = moved.json()["queue"]
    removed_target = next(item for item in moved_snapshot["items"] if item["track_id"] == 102)
    removed = client.delete(
        "/api/queue/0",
        params={
            "station_id": 1,
            "item_id": removed_target["id"],
            "expected_revision": moved_snapshot["revision"],
        },
    )
    assert removed.status_code == 200
    assert removed.json()["runtime_acknowledgement"]["persisted"] is True
    assert [item["track_id"] for item in removed.json()["queue"]["items"]] == [101]


def test_queue_stale_snapshot_rejects_concurrent_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)
    client.post("/api/queue/push", json={"station_id": 1, "track_id": 201})
    client.post("/api/queue/push", json={"station_id": 1, "track_id": 202})
    stale = client.get("/api/queue?station_id=1").json()
    target = next(item for item in stale["items"] if item["track_id"] == 201)

    # Another operator changes the active queue before the original request arrives.
    client.post("/api/queue/push", json={"station_id": 1, "track_id": 203})
    rejected = client.delete(
        f"/api/queue/{target['queue_index']}",
        params={
            "station_id": 1,
            "item_id": target["id"],
            "expected_revision": stale["revision"],
        },
    )
    assert rejected.status_code == 409
    after = client.get("/api/queue?station_id=1").json()
    assert any(item["id"] == target["id"] for item in after["items"])


def test_queue_mutations_reject_current_playing_item(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)
    client.post("/api/queue/push", json={"station_id": 1, "track_id": 301})
    initial = client.get("/api/queue?station_id=1").json()
    item = next(item for item in initial["items"] if item["track_id"] == 301)
    conn = get_connection()
    conn.execute("UPDATE queue_items SET status='playing' WHERE id=?", (item["id"],))
    conn.commit()
    conn.close()
    playing = client.get("/api/queue?station_id=1").json()

    rejected = client.delete(
        f"/api/queue/{item['queue_index']}",
        params={
            "station_id": 1,
            "item_id": item["id"],
            "expected_revision": playing["revision"],
        },
    )
    assert rejected.status_code == 409
    assert "current playing" in rejected.json()["detail"]


def test_queue_acknowledgement_reports_failed_websocket_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    from app.ws.broadcaster import broadcaster

    monkeypatch.setattr(broadcaster, "on_queue_changed", lambda *_args, **_kwargs: False)
    response = TestClient(app).post(
        "/api/queue/push", json={"station_id": 1, "track_id": 401}
    )
    assert response.status_code == 200
    assert response.json()["persistence"]["committed"] is True
    assert response.json()["runtime_acknowledgement"]["queue_event_published"] is False


def test_queue_push_rolls_back_when_outbox_write_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    conn.execute(
        "CREATE TRIGGER fail_queue_outbox BEFORE INSERT ON command_outbox "
        "BEGIN SELECT RAISE(ABORT, 'outbox unavailable'); END"
    )
    conn.commit()
    conn.close()

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/queue/push", json={"station_id": 1, "track_id": 501}
    )
    assert response.status_code == 500
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM queue_items WHERE station_id=1 AND track_id=501"
    ).fetchone()
    conn.close()
    assert row["count"] == 0
