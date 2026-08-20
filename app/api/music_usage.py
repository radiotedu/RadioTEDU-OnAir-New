from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import require_permission
from app.db import get_connection, init_db
from app.services.music_usage import MusicUsageService
from app.services.juke_music_usage import JukeLedgerIntegrityError, list_juke_music_usage

router = APIRouter()


class TrackBroadcastMetadataPayload(BaseModel):
    version: str = ""
    composer: str = ""
    lyricist: str = ""
    phonogram_producer: str = ""
    label: str = ""
    isrc: str = ""
    source_reference: str = ""
    rights_reference: str = ""
    source_type: str = ""
    notes: str = Field(default="", max_length=2000)


class MonthlyClosePayload(BaseModel):
    year: int
    month: int
    closed_by: str = "operator"
    include_juke: bool = True


def _juke_entries(conn, *, date_from: str | None, date_to: str | None, limit: int):
    try:
        return list_juke_music_usage(
            conn, date_from=date_from, date_to=date_to, limit=limit
        )
    except JukeLedgerIntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="juke_compliance_ledger_integrity_failed"
        ) from exc


@router.get("/api/music-usage")
def list_music_usage(
    station_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
    include_juke: bool = False,
    _user=Depends(require_permission("logs.view")),
):
    init_db()
    conn = get_connection()
    try:
        safe_limit = max(1, min(int(limit), 10000))
        items = MusicUsageService(conn).list_entries(
            station_id=station_id, date_from=date_from, date_to=date_to, limit=safe_limit
        )
        juke_status = None
        if include_juke:
            juke_items, juke_status = _juke_entries(
                conn, date_from=date_from, date_to=date_to, limit=safe_limit
            )
            items.extend(juke_items)
            items.sort(key=lambda entry: (str(entry.get("broadcast_at") or ""), str(entry.get("log_id") or "")))
            items = items[:safe_limit]
        return {"items": items, "sources": {"juke_local": juke_status}}
    finally:
        conn.close()


@router.get("/api/music-usage/export")
def export_music_usage(
    station_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    format: str = "csv",
    include_juke: bool = False,
    _user=Depends(require_permission("logs.view")),
):
    init_db()
    conn = get_connection()
    try:
        service = MusicUsageService(conn)
        entries = service.list_entries(
            station_id=station_id,
            date_from=date_from,
            date_to=date_to,
            limit=None,
        )
        if include_juke:
            juke_items, _status = _juke_entries(
                conn, date_from=date_from, date_to=date_to, limit=10000
            )
            entries.extend(juke_items)
            entries.sort(key=lambda entry: (str(entry.get("broadcast_at") or ""), str(entry.get("log_id") or "")))
            entries = entries[:10000]
        if str(format or "csv").lower() == "json":
            return {"items": entries}
        if str(format or "csv").lower() != "csv":
            raise HTTPException(status_code=400, detail="format must be csv or json")
        station_token = str(station_id) if station_id is not None else "all"
        return PlainTextResponse(
            service.csv_text(entries),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="radiotedu-music-usage-{station_token}.csv"'
            },
        )
    finally:
        conn.close()


@router.get("/api/music-usage/track-metadata/{track_id}")
def get_track_broadcast_metadata(
    track_id: int,
    _user=Depends(require_permission("library.view")),
):
    init_db()
    conn = get_connection()
    try:
        return MusicUsageService(conn).get_track_metadata(track_id) or {"track_id": int(track_id)}
    finally:
        conn.close()


@router.put("/api/music-usage/track-metadata/{track_id}")
def save_track_broadcast_metadata(
    track_id: int,
    payload: TrackBroadcastMetadataPayload,
    _user=Depends(require_permission("library.edit")),
):
    init_db()
    conn = get_connection()
    try:
        exists = conn.execute("SELECT 1 FROM tracks WHERE id=?", (int(track_id),)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="track not found")
        return MusicUsageService(conn).upsert_track_metadata(track_id, payload.model_dump())
    finally:
        conn.close()


@router.post("/api/music-usage/monthly-close")
def close_music_usage_month(
    payload: MonthlyClosePayload,
    _user=Depends(require_permission("logs.view")),
):
    if payload.month < 1 or payload.month > 12:
        raise HTTPException(status_code=400, detail="month must be 1..12")
    if payload.year < 2000 or payload.year > date.today().year + 1:
        raise HTTPException(status_code=400, detail="invalid year")
    init_db()
    conn = get_connection()
    try:
        juke_entries: list[dict] = []
        if payload.include_juke:
            period_start = f"{payload.year:04d}-{payload.month:02d}-01"
            if payload.month == 12:
                period_end = f"{payload.year + 1:04d}-01-01"
            else:
                period_end = f"{payload.year:04d}-{payload.month + 1:02d}-01"
            juke_entries, _status = _juke_entries(
                conn, date_from=period_start, date_to=period_end, limit=10000
            )
        return MusicUsageService(conn).close_month(
            year=payload.year,
            month=payload.month,
            closed_by=payload.closed_by,
            extra_entries=juke_entries,
        )
    finally:
        conn.close()


@router.get("/api/music-usage/monthly-closures")
def list_music_usage_closures(
    limit: int = 24,
    _user=Depends(require_permission("logs.view")),
):
    init_db()
    conn = get_connection()
    try:
        safe_limit = max(1, min(int(limit), 120))
        rows = conn.execute(
            "SELECT * FROM music_usage_month_closures ORDER BY period_key DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}
    finally:
        conn.close()
