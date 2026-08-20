import json

from fastapi import APIRouter
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.engine.priority import choose_source
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.queue_repo import QueueRepository
from app.repositories.schedule_repo import ScheduleRepository

router = APIRouter()


class QueuePush(BaseModel):
    station_id: int
    track_id: int


@router.post("/api/queue/push")
def push(payload: QueuePush):
    init_db()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        queue_repo = QueueRepository(conn)
        item_id, created = queue_repo.enqueue_or_get_existing(
            station_id=payload.station_id,
            track_id=payload.track_id,
            dedupe_key=f"queue:{payload.station_id}:{payload.track_id}",
            manage_transaction=False,
        )
        if created:
            conn.cursor().execute(
                "INSERT INTO command_outbox (station_id, command_type, payload_json, status) "
                "VALUES (?, 'queue_push', ?, 'pending')",
                (payload.station_id, json.dumps(payload.model_dump())),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        from app.api.legacy import _queue_mutation_acknowledgement

        acknowledgement = _queue_mutation_acknowledgement(payload.station_id)
    except Exception:
        acknowledgement = {
            "persistence": {"committed": True},
            "worker_acknowledgement": {
                "observed": False,
                "state": "unknown",
                "detail": "The worker acknowledgement could not be read.",
            },
            "runtime_acknowledgement": {
                "persisted": True,
                "queue_event_published": False,
                "worker_running": False,
                "observed": False,
                "current_track_interrupted": False,
            }
        }
    return {
        "ok": True,
        "item_id": item_id,
        "deduped": not created,
        **acknowledgement,
    }


@router.get("/api/playout/state")
def state(station_id: int):
    init_db()
    conn = get_connection()
    queue_repo = QueueRepository(conn)
    ad_repo = AdBreakRepository(conn)
    schedule_repo = ScheduleRepository(conn)
    pending = queue_repo.next_pending(station_id)
    due_ad = ad_repo.next_due(station_id)
    ready_schedule = schedule_repo.next_ready(station_id)
    manual_count = 1 if pending else 0
    next_source = choose_source(
        manual_count=manual_count,
        ad_due=bool(due_ad),
        schedule_ready=bool(ready_schedule),
        fallback_ready=True,
    )
    return {
        "station_id": station_id,
        "next_source": next_source,
        "manual_count": manual_count,
        "ad_due": bool(due_ad),
        "schedule_ready": bool(ready_schedule),
    }


@router.get("/api/queue/items")
def queue_items(station_id: int, limit: int = 20):
    init_db()
    conn = get_connection()
    queue_repo = QueueRepository(conn)
    rows = queue_repo.list_recent(station_id=station_id, limit=limit)
    items = [
        {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]),
            "track_id": int(row["track_id"]),
            "position": int(row["position"]),
            "status": str(row["status"]),
            "enqueued_at": row["enqueued_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "title": str(row["title"]),
            "artist": str(row["artist"]),
        }
        for row in rows
    ]
    return {"station_id": station_id, "items": items}
