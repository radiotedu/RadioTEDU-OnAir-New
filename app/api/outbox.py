import json

from fastapi import APIRouter

from app.db import get_connection, init_db
from app.repositories.outbox_repo import OutboxRepository

router = APIRouter()


@router.get("/api/outbox/items")
def list_outbox_items(station_id: int | None = None, status: str = "", limit: int = 50):
    init_db()
    conn = get_connection()
    repo = OutboxRepository(conn)
    rows = repo.list_recent(station_id=station_id, status=status, limit=limit)
    items = []
    for row in rows:
        payload_json = str(row["payload_json"] or "{}")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {"raw": payload_json}
        items.append(
            {
                "id": int(row["id"]),
                "station_id": int(row["station_id"]),
                "command_type": str(row["command_type"]),
                "status": str(row["status"]),
                "attempt_count": int(row["attempt_count"]),
                "available_at": str(row["available_at"]),
                "created_at": str(row["created_at"]),
                "payload": payload,
            }
        )
    return {"items": items}


@router.post("/api/outbox/claim-next")
def claim_next_outbox_item(station_id: int):
    init_db()
    conn = get_connection()
    repo = OutboxRepository(conn)
    row = repo.claim_next(station_id=station_id)
    if row is None:
        return {"item": None}
    payload_json = str(row["payload_json"] or "{}")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        payload = {"raw": payload_json}
    return {
        "item": {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]),
            "command_type": str(row["command_type"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "payload": payload,
        }
    }


@router.post("/api/outbox/{item_id}/done")
def mark_outbox_done(item_id: int):
    init_db()
    conn = get_connection()
    repo = OutboxRepository(conn)
    repo.mark_done(item_id)
    return {"ok": True}


@router.post("/api/outbox/{item_id}/failed")
def mark_outbox_failed(item_id: int):
    init_db()
    conn = get_connection()
    repo = OutboxRepository(conn)
    repo.mark_failed(item_id)
    return {"ok": True}
