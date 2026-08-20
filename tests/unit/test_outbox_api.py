from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


def test_outbox_list_and_claim_and_done_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    push_res = client.post("/api/queue/push", json={"station_id": 1, "track_id": 77})
    assert push_res.status_code == 200

    list_res = client.get("/api/outbox/items", params={"station_id": 1, "status": "pending"})
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert len(items) == 1
    item_id = int(items[0]["id"])

    claim_res = client.post("/api/outbox/claim-next", params={"station_id": 1})
    assert claim_res.status_code == 200
    assert claim_res.json()["item"] is not None
    assert int(claim_res.json()["item"]["id"]) == item_id

    processing_list = client.get(
        "/api/outbox/items", params={"station_id": 1, "status": "processing"}
    )
    assert processing_list.status_code == 200
    assert len(processing_list.json()["items"]) == 1

    done_res = client.post(f"/api/outbox/{item_id}/done")
    assert done_res.status_code == 200
    done_list = client.get("/api/outbox/items", params={"station_id": 1, "status": "done"})
    assert done_list.status_code == 200
    assert len(done_list.json()["items"]) == 1


def test_outbox_mark_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    push_res = client.post("/api/queue/push", json={"station_id": 2, "track_id": 88})
    assert push_res.status_code == 200
    claim_res = client.post("/api/outbox/claim-next", params={"station_id": 2})
    item = claim_res.json()["item"]
    assert item is not None
    item_id = int(item["id"])

    fail_res = client.post(f"/api/outbox/{item_id}/failed")
    assert fail_res.status_code == 200
    failed_list = client.get("/api/outbox/items", params={"station_id": 2, "status": "failed"})
    assert failed_list.status_code == 200
    assert len(failed_list.json()["items"]) == 1
