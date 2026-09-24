import math
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.audio.audio_processing import probe_duration
from app.db import get_connection, get_read_connection, init_db
from app.engine.ad_policy import station_ads_enabled
from app.repositories.ad_break_repo import AdBreakRepository
from app.services.product_media_catalog import (
    ProductCatalogError,
    get_product_media_catalog_service,
)

router = APIRouter()


class AdItemCreate(BaseModel):
    station_id: int
    track_id: int
    due_at: str
    priority: int = 0


class AdCatalogSyncPayload(BaseModel):
    station_ids: list[int] = Field(min_length=1, max_length=64)


def _catalog_read_connection():
    try:
        return get_read_connection(timeout_seconds=1.5)
    except sqlite3.OperationalError as exc:
        if any(token in str(exc).casefold() for token in ("locked", "busy")):
            raise HTTPException(
                status_code=503,
                detail="ads_catalog_busy",
                headers={"Retry-After": "1"},
            ) from exc
        raise


def _catalog_path_key(value: str) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").casefold()


def _read_ad_catalog():
    try:
        return get_product_media_catalog_service().list_items("ads", limit=500)
    except ProductCatalogError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _probe_ad_duration(path: Path) -> float:
    """Retry one transient probe failure before rejecting an approved asset."""
    for attempt in range(2):
        try:
            duration = float(probe_duration(str(path), timeout_seconds=10.0) or 0)
        except (OSError, TypeError, ValueError):
            duration = 0.0
        if math.isfinite(duration) and duration > 0:
            return duration
        if attempt == 0:
            time.sleep(0.25)
    return 0.0


@router.get("/api/ads/catalog")
def list_ad_catalog(station_id: int):
    """Expose the approved Ads-folder generation and prepared track IDs."""
    catalog = _read_ad_catalog()
    conn = _catalog_read_connection()
    try:
        stations = [
            {"id": int(row["id"]), "name": str(row["name"] or f"Station #{row['id']}")}
            for row in conn.execute("SELECT id, name FROM stations ORDER BY name COLLATE NOCASE, id")
        ]
        station_ids = {row["id"] for row in stations}
        if int(station_id) not in station_ids:
            raise HTTPException(status_code=404, detail="station_not_found")

        track_rows = conn.execute(
            "SELECT id, station_id, title, artist, duration, file_path "
            "FROM tracks WHERE is_active=1 "
            "AND LOWER(COALESCE(track_type,'')) IN ('ad','ads','advertising') "
            "ORDER BY title COLLATE NOCASE, id"
        ).fetchall()
        track_ids_by_path: dict[str, dict[str, int]] = {}
        for row in track_rows:
            key = _catalog_path_key(str(row["file_path"] or ""))
            if key:
                track_ids_by_path.setdefault(key, {})[str(int(row["station_id"]))] = int(row["id"])

        catalog_items = []
        for item in catalog.get("items", []):
            path_key = _catalog_path_key(str(item.get("path") or ""))
            catalog_items.append(
                {
                    "relative_path": str(item.get("relative_path") or ""),
                    "file_name": str(item.get("file_name") or ""),
                    "title": str(item.get("title") or ""),
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "generation": int(item.get("generation") or 0),
                    "stale": bool(item.get("stale")),
                    "track_ids_by_station": track_ids_by_path.get(path_key, {}),
                }
            )
        selected_tracks = [
            {
                "id": int(row["id"]),
                "station_id": int(row["station_id"]),
                "title": str(row["title"] or ""),
                "artist": str(row["artist"] or ""),
                "duration": float(row["duration"] or 0),
            }
            for row in track_rows
            if int(row["station_id"]) == int(station_id)
        ]
        return {
            "station_id": int(station_id),
            "generation": int(catalog.get("generation") or 0),
            "catalog_items": catalog_items,
            "tracks": selected_tracks,
            "stations": stations,
        }
    finally:
        conn.close()


@router.post("/api/ads/catalog/sync")
def sync_ad_catalog(payload: AdCatalogSyncPayload):
    """Prepare only stable Ads-folder files as non-autoplay ad tracks."""
    catalog = _read_ad_catalog()
    items = list(catalog.get("items") or [])
    if not items:
        raise HTTPException(status_code=409, detail="ads_catalog_empty")
    if any(bool(item.get("stale")) or not str(item.get("path") or "") for item in items):
        raise HTTPException(status_code=409, detail="ads_catalog_rescan_required")

    prepared = []
    for item in items:
        try:
            path = Path(str(item["path"])).resolve(strict=True)
            stat = path.stat()
        except OSError as exc:
            raise HTTPException(status_code=409, detail="ads_catalog_item_not_accessible") from exc
        if (
            not path.is_file()
            or int(stat.st_size) != int(item.get("size_bytes") or 0)
            or int(stat.st_mtime_ns) != int(item.get("modified_ns") or stat.st_mtime_ns)
        ):
            raise HTTPException(status_code=409, detail="ads_catalog_rescan_required")
        duration = _probe_ad_duration(path)
        if duration <= 0:
            raise HTTPException(status_code=409, detail="ads_catalog_audio_invalid")
        prepared.append(
            {
                "path": str(path),
                "title": Path(str(item.get("file_name") or path.name)).stem,
                "duration": duration,
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )

    station_ids = list(dict.fromkeys(int(value) for value in payload.station_ids))
    if any(value <= 0 for value in station_ids):
        raise HTTPException(status_code=422, detail="invalid_station_id")
    try:
        conn = get_connection(timeout_seconds=2.0)
    except sqlite3.OperationalError as exc:
        if any(token in str(exc).casefold() for token in ("locked", "busy")):
            raise HTTPException(
                status_code=503,
                detail="ads_catalog_write_busy",
                headers={"Retry-After": "1"},
            ) from exc
        raise
    created = 0
    reused = 0
    tracks = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        known = {
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM stations WHERE id IN ({','.join('?' for _ in station_ids)})",
                tuple(station_ids),
            )
        }
        if known != set(station_ids):
            raise HTTPException(status_code=404, detail="station_not_found")

        for station_id in station_ids:
            for item in prepared:
                existing = conn.execute(
                    "SELECT id, track_type FROM tracks WHERE station_id=? "
                    "AND LOWER(file_path)=LOWER(?) ORDER BY id LIMIT 1",
                    (station_id, item["path"]),
                ).fetchone()
                if existing:
                    if str(existing["track_type"] or "").casefold() not in {"ad", "ads", "advertising"}:
                        raise HTTPException(status_code=409, detail="ads_catalog_track_type_conflict")
                    track_id = int(existing["id"])
                    conn.execute(
                        "UPDATE tracks SET title=?, artist='RadioTEDU', album='RadioTEDU Advertising', "
                        "genre='Advertising', language='', duration=?, bpm=0, track_type='ad', "
                        "is_active=1, exclude_from_autoplay=1, managed_file_size=?, managed_file_mtime_ns=? "
                        "WHERE id=?",
                        (item["title"], item["duration"], item["size"], item["mtime_ns"], track_id),
                    )
                    reused += 1
                else:
                    cursor = conn.execute(
                        "INSERT INTO tracks (station_id, title, artist, album, genre, language, duration, bpm, "
                        "track_type, is_active, exclude_from_autoplay, file_path, managed_file_size, managed_file_mtime_ns) "
                        "VALUES (?, ?, 'RadioTEDU', 'RadioTEDU Advertising', 'Advertising', '', ?, 0, 'ad', 1, 1, ?, ?, ?)",
                        (
                            station_id,
                            item["title"],
                            item["duration"],
                            item["path"],
                            item["size"],
                            item["mtime_ns"],
                        ),
                    )
                    track_id = int(cursor.lastrowid)
                    created += 1
                tracks.append(
                    {"id": track_id, "station_id": station_id, "title": item["title"]}
                )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if any(token in str(exc).casefold() for token in ("locked", "busy")):
            raise HTTPException(
                status_code=503,
                detail="ads_catalog_write_busy",
                headers={"Retry-After": "1"},
            ) from exc
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "ok": True,
        "station_ids": station_ids,
        "asset_count": len(prepared),
        "created": created,
        "reused": reused,
        "tracks": tracks,
    }


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
    try:
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
    finally:
        conn.close()
