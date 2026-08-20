from fastapi import APIRouter
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.repositories.schedule_repo import ScheduleRepository

router = APIRouter()


class ScheduleItemCreate(BaseModel):
    station_id: int
    track_id: int
    play_at: str
    window_end: str | None = None


@router.post("/api/schedule/items")
def create_schedule_item(payload: ScheduleItemCreate):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    item_id = repo.enqueue(
        station_id=payload.station_id,
        track_id=payload.track_id,
        play_at=payload.play_at,
        window_end=payload.window_end,
    )
    return {"ok": True, "item_id": item_id}


@router.get("/api/schedule/items")
def list_schedule_items(station_id: int, limit: int = 20):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    rows = repo.list_recent(station_id=station_id, limit=limit)
    items = [
        {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]),
            "track_id": int(row["track_id"]),
            "play_at": str(row["play_at"]),
            "window_end": row["window_end"],
            "status": str(row["status"]),
            "title": str(row["title"]),
            "artist": str(row["artist"]),
        }
        for row in rows
    ]
    return {"station_id": station_id, "items": items}
