from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import get_connection, init_db
from app.engine.broadcast_plan_policy import resolve_song_ad_plans, song_ad_progress
from app.services.broadcast_planner import (
    _plan_days,
    cancel_future_occurrences,
    materialize_broadcast_plans,
)

router = APIRouter()


class BroadcastPlanPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    plan_type: Literal["ad", "sweeper", "recorded_program"]
    source_station_id: int = Field(gt=0)
    track_id: int = Field(gt=0)
    station_ids: list[int] = Field(min_items=1, max_items=24)
    starts_on: date
    ends_on: date
    weekdays: list[int] = Field(min_items=1, max_items=7)
    local_start: str
    local_end: str
    timezone: str = "Europe/Istanbul"
    repeat_every_minutes: int = Field(default=0, ge=0, le=1440)
    sweeper_every_songs: int = Field(default=2, ge=1, le=100)
    cadence_mode: Literal["time", "songs"] = "time"
    repeat_every_songs: int = Field(default=10, ge=1, le=100)
    play_window_minutes: int = Field(default=15, ge=1, le=240)
    priority: int = Field(default=0, ge=-100, le=1000)
    enabled: bool = True


class EnabledPayload(BaseModel):
    enabled: bool


def _validate_payload(payload: BroadcastPlanPayload) -> tuple[list[int], time, time]:
    if payload.ends_on < payload.starts_on:
        raise HTTPException(status_code=422, detail="ends_on_before_starts_on")
    station_ids = sorted({int(value) for value in payload.station_ids if int(value) > 0})
    if not station_ids or len(station_ids) != len(payload.station_ids):
        raise HTTPException(status_code=422, detail="invalid_station_ids")
    if any(int(day) not in range(1, 8) for day in payload.weekdays):
        raise HTTPException(status_code=422, detail="weekdays_must_use_iso_1_to_7")
    if payload.plan_type == "sweeper" and payload.repeat_every_minutes:
        raise HTTPException(status_code=422, detail="sweeper_uses_song_interval")
    if payload.plan_type != "ad" and payload.cadence_mode != "time":
        raise HTTPException(status_code=422, detail="song_cadence_is_for_ads_only")
    if payload.plan_type == "ad" and payload.cadence_mode == "songs" and payload.repeat_every_minutes:
        raise HTTPException(status_code=422, detail="song_cadence_cannot_repeat_by_minutes")
    if payload.repeat_every_minutes and payload.repeat_every_minutes < 5:
        raise HTTPException(status_code=422, detail="repeat_interval_minimum_is_5_minutes")
    try:
        ZoneInfo(payload.timezone)
        start = time.fromisoformat(payload.local_start)
        end = time.fromisoformat(payload.local_end)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(status_code=422, detail="invalid_time_or_timezone") from None
    if start.second or end.second:
        raise HTTPException(status_code=422, detail="time_precision_must_be_minutes")
    return station_ids, start, end


def _get_track(conn, source_station_id: int, track_id: int):
    row = conn.execute(
        "SELECT * FROM tracks WHERE id=? AND station_id=? AND is_active=1",
        (int(track_id), int(source_station_id)),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="active_track_not_found_in_source_station")
    if not str(row["file_path"] or "").strip():
        raise HTTPException(status_code=409, detail="track_has_no_media_file")
    return row


def _copy_track_to_station(conn, source, source_station_id: int, station_id: int) -> int:
    if int(source_station_id) == int(station_id):
        return int(source["id"])
    path = str(source["file_path"] or "").strip()
    existing = conn.execute(
        "SELECT id FROM tracks WHERE station_id=? AND file_path=? AND is_active=1 "
        "AND LOWER(COALESCE(track_type,'music'))=LOWER(?) ORDER BY id LIMIT 1",
        (int(station_id), path, str(source["track_type"] or "music")),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(tracks)").fetchall()]
    copy_columns = [column for column in columns if column not in {"id", "station_id"}]
    quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in ["station_id", *copy_columns])
    placeholders = ", ".join("?" for _ in ["station_id", *copy_columns])
    values = [int(station_id), *(source[column] for column in copy_columns)]
    cur = conn.cursor()
    cur.execute(f"INSERT INTO tracks ({quoted}) VALUES ({placeholders})", values)
    return int(cur.lastrowid)


def _track_type_matches(plan_type: str, track_type: str) -> bool:
    actual = str(track_type or "music").strip().lower()
    if plan_type == "ad":
        return actual == "ad"
    if plan_type == "sweeper":
        return actual == "jingle"
    return actual not in {"ad", "jingle", "startup"}


def _save_plan(conn, payload: BroadcastPlanPayload, plan_id: int | None = None) -> int:
    station_ids, _, _ = _validate_payload(payload)
    source = _get_track(conn, payload.source_station_id, payload.track_id)
    if not _track_type_matches(payload.plan_type, str(source["track_type"] or "music")):
        raise HTTPException(
            status_code=409,
            detail=f"track_type_must_match_{payload.plan_type}",
        )
    placeholders = ",".join("?" for _ in station_ids)
    stations = conn.execute(
        f"SELECT id FROM stations WHERE id IN ({placeholders})", station_ids
    ).fetchall()
    if {int(row["id"]) for row in stations} != set(station_ids):
        raise HTTPException(status_code=404, detail="one_or_more_stations_not_found")

    if plan_id is not None:
        existing = conn.execute("SELECT id FROM broadcast_plans WHERE id=?", (int(plan_id),)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="broadcast_plan_not_found")
        cancel_future_occurrences(conn, int(plan_id))
        conn.execute("DELETE FROM broadcast_plan_targets WHERE plan_id=?", (int(plan_id),))
        conn.execute(
            "UPDATE broadcast_plans SET name=?, plan_type=?, source_station_id=?, source_track_id=?, "
            "starts_on=?, ends_on=?, weekdays_json=?, local_start=?, local_end=?, timezone=?, "
            "repeat_every_minutes=?, sweeper_every_songs=?, cadence_mode=?, repeat_every_songs=?, "
            "play_window_minutes=?, priority=?, enabled=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                payload.name.strip(), payload.plan_type, int(payload.source_station_id), int(payload.track_id),
                payload.starts_on.isoformat(), payload.ends_on.isoformat(),
                json.dumps(sorted(set(payload.weekdays))), payload.local_start, payload.local_end,
                payload.timezone, int(payload.repeat_every_minutes), int(payload.sweeper_every_songs),
                payload.cadence_mode, int(payload.repeat_every_songs), int(payload.play_window_minutes),
                int(payload.priority), int(payload.enabled), int(plan_id),
            ),
        )
        saved_id = int(plan_id)
    else:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO broadcast_plans "
            "(name, plan_type, source_station_id, source_track_id, starts_on, ends_on, weekdays_json, "
            "local_start, local_end, timezone, repeat_every_minutes, sweeper_every_songs, "
            "cadence_mode, repeat_every_songs, "
            "play_window_minutes, priority, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.name.strip(), payload.plan_type, int(payload.source_station_id), int(payload.track_id),
                payload.starts_on.isoformat(), payload.ends_on.isoformat(),
                json.dumps(sorted(set(payload.weekdays))), payload.local_start, payload.local_end,
                payload.timezone, int(payload.repeat_every_minutes), int(payload.sweeper_every_songs),
                payload.cadence_mode, int(payload.repeat_every_songs), int(payload.play_window_minutes),
                int(payload.priority), int(payload.enabled),
            ),
        )
        saved_id = int(cur.lastrowid)

    for station_id in station_ids:
        target_track_id = _copy_track_to_station(
            conn, source, payload.source_station_id, station_id
        )
        conn.execute(
            "INSERT INTO broadcast_plan_targets(plan_id, station_id, track_id, enabled) "
            "VALUES (?, ?, ?, 1)",
            (saved_id, station_id, target_track_id),
        )
    conn.commit()
    if payload.plan_type != "sweeper" and payload.enabled:
        materialize_broadcast_plans(conn, horizon_days=14)
    return saved_id


def _serialize_plan(conn, row) -> dict:
    source_track = conn.execute(
        "SELECT COALESCE(title,'') AS title, COALESCE(artist,'') AS artist "
        "FROM tracks WHERE id=?",
        (int(row["source_track_id"]),),
    ).fetchone()
    targets = conn.execute(
        "SELECT t.station_id, t.track_id, COALESCE(s.name,'') AS station_name, "
        "COALESCE(tr.title,'') AS track_title, COALESCE(tr.artist,'') AS track_artist "
        "FROM broadcast_plan_targets t LEFT JOIN stations s ON s.id=t.station_id "
        "LEFT JOIN tracks tr ON tr.id=t.track_id WHERE t.plan_id=? AND t.enabled=1 "
        "ORDER BY s.name, t.station_id",
        (int(row["id"]),),
    ).fetchall()
    occurrence_count = conn.execute(
        "SELECT COUNT(*) AS amount FROM broadcast_plan_occurrences "
        "WHERE plan_id=? AND status='pending'",
        (int(row["id"]),),
    ).fetchone()["amount"]
    song_ad_count = conn.execute(
        "SELECT COUNT(*) AS amount FROM ad_break_items WHERE status='pending' "
        "AND dedupe_key LIKE ?",
        (f"broadcast-plan:{int(row['id'])}:song:%",),
    ).fetchone()["amount"]
    return {
        "id": int(row["id"]), "name": str(row["name"]), "plan_type": str(row["plan_type"]),
        "source_station_id": int(row["source_station_id"]), "source_track_id": int(row["source_track_id"]),
        "source_track_title": str(source_track["title"] if source_track else ""),
        "source_track_artist": str(source_track["artist"] if source_track else ""),
        "starts_on": str(row["starts_on"]), "ends_on": str(row["ends_on"]),
        "weekdays": sorted(_plan_days(row)), "local_start": str(row["local_start"]),
        "local_end": str(row["local_end"]), "timezone": str(row["timezone"]),
        "repeat_every_minutes": int(row["repeat_every_minutes"]),
        "cadence_mode": str(row["cadence_mode"] or "time"),
        "repeat_every_songs": int(row["repeat_every_songs"] or 10),
        "sweeper_every_songs": int(row["sweeper_every_songs"]),
        "play_window_minutes": int(row["play_window_minutes"]), "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "pending_occurrences": int(occurrence_count or 0) + int(song_ad_count or 0),
        "targets": [dict(target) for target in targets],
    }


@router.get("/api/broadcast-plans")
def list_broadcast_plans(station_id: int | None = None):
    init_db()
    conn = get_connection()
    try:
        if station_id is None:
            rows = conn.execute("SELECT * FROM broadcast_plans ORDER BY enabled DESC, id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT p.* FROM broadcast_plans p JOIN broadcast_plan_targets t ON t.plan_id=p.id "
                "WHERE t.station_id=? GROUP BY p.id ORDER BY p.enabled DESC, p.id DESC",
                (int(station_id),),
            ).fetchall()
        return {"plans": [_serialize_plan(conn, row) for row in rows]}
    finally:
        conn.close()


@router.get("/api/broadcast-plans/ad-status")
def broadcast_ad_status(station_id: int):
    """Return the next configured ad and its live song or clock countdown."""
    init_db()
    conn = get_connection()
    try:
        plans = resolve_song_ad_plans(conn, int(station_id))
        statuses = []
        for plan in plans:
            progress = song_ad_progress(conn, plan, int(station_id))
            statuses.append({**plan, **progress})
        if statuses:
            statuses.sort(key=lambda item: (not item["due"], int(item["remaining_songs"]), -int(item["priority"])))
            selected = statuses[0]
            return {
                "station_id": int(station_id), "mode": "songs", "due": bool(selected["due"]),
                "title": selected["track_title"], "artist": selected["track_artist"],
                "plan_name": selected["name"], "priority": int(selected["priority"]),
                "remaining_songs": int(selected["remaining_songs"]),
                "due_ads": sum(1 for item in statuses if item["due"]),
            }

        item = conn.execute(
            "SELECT a.due_at, a.priority, COALESCE(t.title,'') AS title, "
            "COALESCE(t.artist,'') AS artist FROM ad_break_items a "
            "LEFT JOIN tracks t ON t.id=a.track_id "
            "WHERE a.station_id=? AND a.status='pending' "
            "ORDER BY datetime(a.due_at), a.priority DESC, a.id LIMIT 1",
            (int(station_id),),
        ).fetchone()
        if item:
            due_at = datetime.fromisoformat(str(item["due_at"]).replace("Z", "+00:00"))
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            remaining_seconds = max(0, int((due_at - datetime.now(timezone.utc)).total_seconds()))
            return {
                "station_id": int(station_id), "mode": "time", "due": remaining_seconds == 0,
                "title": str(item["title"] or ""), "artist": str(item["artist"] or ""),
                "priority": int(item["priority"] or 0), "remaining_seconds": remaining_seconds,
            }
        return {"station_id": int(station_id), "mode": "none", "due": False}
    finally:
        conn.close()


@router.post("/api/broadcast-plans")
def create_broadcast_plan(payload: BroadcastPlanPayload):
    init_db()
    conn = get_connection()
    try:
        plan_id = _save_plan(conn, payload)
        return {"ok": True, "plan_id": plan_id, "plan": list_broadcast_plans_for_connection(conn, plan_id)}
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail="broadcast_plan_save_failed") from exc
    finally:
        conn.close()


def list_broadcast_plans_for_connection(conn, plan_id: int) -> dict:
    row = conn.execute("SELECT * FROM broadcast_plans WHERE id=?", (int(plan_id),)).fetchone()
    if row is None:
        return {}
    return _serialize_plan(conn, row)


@router.put("/api/broadcast-plans/{plan_id}")
def update_broadcast_plan(plan_id: int, payload: BroadcastPlanPayload):
    init_db()
    conn = get_connection()
    try:
        saved_id = _save_plan(conn, payload, int(plan_id))
        return {"ok": True, "plan_id": saved_id, "plan": list_broadcast_plans_for_connection(conn, saved_id)}
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail="broadcast_plan_save_failed") from exc
    finally:
        conn.close()


@router.post("/api/broadcast-plans/{plan_id}/enabled")
def set_broadcast_plan_enabled(plan_id: int, payload: EnabledPayload):
    init_db()
    conn = get_connection()
    try:
        row = conn.execute("SELECT id, plan_type FROM broadcast_plans WHERE id=?", (int(plan_id),)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="broadcast_plan_not_found")
        if not payload.enabled:
            cancel_future_occurrences(conn, int(plan_id))
        conn.execute(
            "UPDATE broadcast_plans SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(payload.enabled), int(plan_id)),
        )
        conn.commit()
        if payload.enabled and str(row["plan_type"]) != "sweeper":
            materialize_broadcast_plans(conn, horizon_days=14)
        return {"ok": True, "plan": list_broadcast_plans_for_connection(conn, int(plan_id))}
    finally:
        conn.close()


@router.delete("/api/broadcast-plans/{plan_id}")
def delete_broadcast_plan(plan_id: int):
    init_db()
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM broadcast_plans WHERE id=?", (int(plan_id),)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="broadcast_plan_not_found")
        cancel_future_occurrences(conn, int(plan_id))
        conn.execute("DELETE FROM broadcast_plans WHERE id=?", (int(plan_id),))
        conn.commit()
        return {"ok": True, "deleted": int(plan_id)}
    finally:
        conn.close()
