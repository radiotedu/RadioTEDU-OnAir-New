from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.engine.ad_policy import station_ads_enabled
from app.repositories.ad_break_repo import AdBreakRepository

router = APIRouter()


class AdItemCreate(BaseModel):
    station_id: int
    track_id: int
    due_at: str
    priority: int = 0


@router.post("/api/ads/items")
def create_ad_item(payload: AdItemCreate):
    init_db()
    conn = get_connection()
    try:
        if not station_ads_enabled(conn, payload.station_id):
            raise HTTPException(
                status_code=409, detail="ads_disabled_for_station"
            )
        repo = AdBreakRepository(conn)
        item_id = repo.enqueue(
            station_id=payload.station_id,
            track_id=payload.track_id,
            due_at=payload.due_at,
            priority=payload.priority,
        )
        return {"ok": True, "item_id": item_id}
    finally:
        conn.close()


@router.get("/api/ads/items")
def list_ad_items(station_id: int, limit: int = 20):
    init_db()
    conn = get_connection()
    repo = AdBreakRepository(conn)
    rows = repo.list_recent(station_id=station_id, limit=limit)
    items = [
        {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]),
            "track_id": int(row["track_id"]),
            "due_at": str(row["due_at"]),
            "status": str(row["status"]),
            "priority": int(row["priority"]),
            "title": str(row["title"]),
            "artist": str(row["artist"]),
        }
        for row in rows
    ]
    return {"station_id": station_id, "items": items}
