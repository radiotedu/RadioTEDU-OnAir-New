"""Station-local broadcast plan policy helpers shared by playout workers."""

from __future__ import annotations

import json
import threading
import time as monotonic_time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_CACHE_LOCK = threading.Lock()
_SWEEPER_CACHE: dict[int, tuple[float, dict]] = {}
_AD_CACHE: dict[int, tuple[float, bool]] = {}
_CACHE_TTL_SECONDS = 2.0


def resolve_sweeper_plan(conn, station_id: int, now: datetime | None = None) -> dict:
    """Return whether plans govern this station and the highest-priority active plan."""
    sid = int(station_id)
    cached_at = monotonic_time.monotonic()
    if now is None:
        with _CACHE_LOCK:
            cached = _SWEEPER_CACHE.get(sid)
        if cached and cached_at - cached[0] < _CACHE_TTL_SECONDS:
            return dict(cached[1])
    cur = conn.cursor()
    cur.execute(
        "SELECT p.*, t.track_id AS target_track_id "
        "FROM broadcast_plans p JOIN broadcast_plan_targets t ON t.plan_id=p.id "
        "WHERE p.plan_type='sweeper' AND p.enabled=1 AND t.station_id=? AND t.enabled=1 "
        "ORDER BY p.priority DESC, p.id DESC",
        (sid,),
    )
    rows = list(cur.fetchall())
    if not rows:
        result = {"governed": False, "active": None}
        if now is None:
            with _CACHE_LOCK:
                _SWEEPER_CACHE[sid] = (cached_at, result)
        return result

    governed = False
    for row in rows:
        try:
            zone = ZoneInfo(str(row["timezone"] or "Europe/Istanbul"))
            now_local = (now or datetime.now().astimezone()).astimezone(zone)
            starts_on = str(row["starts_on"])
            ends_on = str(row["ends_on"])
            weekdays_json = str(row["weekdays_json"] or "[]")
            today = now_local.date()
            starts_date = date.fromisoformat(starts_on)
            ends_date = date.fromisoformat(ends_on)
            start_time = time.fromisoformat(str(row["local_start"]))
            end_time = time.fromisoformat(str(row["local_end"]))
            weekdays = {int(value) for value in json.loads(weekdays_json)}
            governed = governed or starts_date <= today <= ends_date
            anchor_days = (today, today - timedelta(days=1))
            if end_time <= start_time and now_local.time() < end_time:
                governed = governed or starts_date <= today - timedelta(days=1) <= ends_date
            active = False
            for anchor_day in anchor_days:
                if not starts_date <= anchor_day <= ends_date or anchor_day.isoweekday() not in weekdays:
                    continue
                anchor_start = datetime.combine(anchor_day, start_time, tzinfo=zone)
                anchor_end = datetime.combine(anchor_day, end_time, tzinfo=zone)
                if anchor_end <= anchor_start:
                    anchor_end += timedelta(days=1)
                if anchor_start <= now_local < anchor_end:
                    active = True
                    break
            if active:
                result = {
                    "governed": True,
                    "active": {
                        "plan_id": int(row["id"]),
                        "interval": max(1, int(row["sweeper_every_songs"] or 2)),
                        "track_id": int(row["target_track_id"]),
                        "mode": "ordered",
                    },
                }
                if now is None:
                    with _CACHE_LOCK:
                        _SWEEPER_CACHE[sid] = (cached_at, result)
                return result
        except (KeyError, TypeError, ValueError):
            continue
    result = {"governed": governed, "active": None}
    if now is None:
        with _CACHE_LOCK:
            _SWEEPER_CACHE[sid] = (cached_at, result)
    return result


def station_has_planned_ad(conn, station_id: int) -> bool:
    """Allow explicit plan-created spots through the normal ad-policy gate."""
    sid = int(station_id)
    checked_at = monotonic_time.monotonic()
    with _CACHE_LOCK:
        cached = _AD_CACHE.get(sid)
    if cached and checked_at - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM ad_break_items a "
        "JOIN broadcast_plan_occurrences o ON o.target_kind='ad' AND o.target_item_id=a.id "
        "JOIN broadcast_plans p ON p.id=o.plan_id "
        "WHERE a.station_id=? AND a.status IN ('pending','playing') "
        "AND p.plan_type='ad' AND p.enabled=1 LIMIT 1",
        (sid,),
    )
    result = cur.fetchone() is not None
    with _CACHE_LOCK:
        _AD_CACHE[sid] = (checked_at, result)
    return result


def resolve_song_ad_plans(conn, station_id: int, now: datetime | None = None) -> list[dict]:
    """Return active song-cadence ad plans for a station, highest priority first."""
    sid = int(station_id)
    rows = conn.execute(
        "SELECT p.*, t.track_id AS target_track_id, "
        "COALESCE(tr.title,'') AS track_title, COALESCE(tr.artist,'') AS track_artist "
        "FROM broadcast_plans p JOIN broadcast_plan_targets t ON t.plan_id=p.id "
        "LEFT JOIN tracks tr ON tr.id=t.track_id "
        "WHERE p.plan_type='ad' AND p.cadence_mode='songs' AND p.enabled=1 "
        "AND t.station_id=? AND t.enabled=1 ORDER BY p.priority DESC, p.id DESC",
        (sid,),
    ).fetchall()
    active: list[dict] = []
    for row in rows:
        try:
            zone = ZoneInfo(str(row["timezone"] or "Europe/Istanbul"))
            current = now or datetime.now().astimezone()
            current_local = current.astimezone(zone)
            starts_on = date.fromisoformat(str(row["starts_on"]))
            ends_on = date.fromisoformat(str(row["ends_on"]))
            start_time = time.fromisoformat(str(row["local_start"]))
            end_time = time.fromisoformat(str(row["local_end"]))
            weekdays = {int(value) for value in json.loads(str(row["weekdays_json"] or "[]"))}
            is_active = False
            for anchor_day in (current_local.date(), current_local.date() - timedelta(days=1)):
                if not starts_on <= anchor_day <= ends_on or anchor_day.isoweekday() not in weekdays:
                    continue
                anchor_start = datetime.combine(anchor_day, start_time, tzinfo=zone)
                anchor_end = datetime.combine(anchor_day, end_time, tzinfo=zone)
                if anchor_end <= anchor_start:
                    anchor_end += timedelta(days=1)
                if anchor_start <= current_local < anchor_end:
                    is_active = True
                    break
            if not is_active:
                continue
            active.append({
                "plan_id": int(row["id"]),
                "name": str(row["name"] or ""),
                "track_id": int(row["target_track_id"]),
                "track_title": str(row["track_title"] or ""),
                "track_artist": str(row["track_artist"] or ""),
                "priority": int(row["priority"] or 0),
                "interval": max(1, int(row["repeat_every_songs"] or 10)),
                "created_at": str(row["created_at"] or ""),
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return active


def song_ad_progress(conn, plan: dict, station_id: int) -> dict:
    """Report completed music since plan creation and whether this cycle is due."""
    plan_id = int(plan["plan_id"])
    sid = int(station_id)
    interval = max(1, int(plan.get("interval") or 10))
    row = conn.execute(
        "SELECT COUNT(*) AS amount FROM queue_items q "
        "JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=? AND q.status='done' "
        "AND LOWER(COALESCE(t.track_type,'music'))='music' "
        "AND q.finished_at IS NOT NULL AND datetime(q.finished_at)>=datetime(?)",
        (sid, str(plan.get("created_at") or "1970-01-01 00:00:00")),
    ).fetchone()
    music_count = int(row["amount"] or 0)
    cycle = music_count // interval
    if cycle < 1:
        return {
            "music_count": music_count,
            "remaining_songs": interval - music_count,
            "cycle": 1,
            "due": False,
            "item_status": "",
        }

    dedupe_key = f"broadcast-plan:{plan_id}:song:{sid}:{cycle}"
    item = conn.execute(
        "SELECT status FROM ad_break_items WHERE station_id=? AND dedupe_key=? "
        "ORDER BY id DESC LIMIT 1",
        (sid, dedupe_key),
    ).fetchone()
    status = str(item["status"] or "") if item else ""
    if status in {"pending", "playing"}:
        return {
            "music_count": music_count, "remaining_songs": 0, "cycle": cycle,
            "due": True, "item_status": status,
        }
    if status in {"done", "failed"}:
        remainder = music_count % interval
        return {
            "music_count": music_count,
            "remaining_songs": interval - remainder if remainder else interval,
            "cycle": cycle + 1,
            "due": False,
            "item_status": status,
        }
    return {
        "music_count": music_count, "remaining_songs": 0, "cycle": cycle,
        "due": True, "item_status": status,
    }


def materialize_song_cadence_ads(conn, station_id: int) -> int:
    """Queue each due song-cadence spot once, at the boundary after a finished song."""
    sid = int(station_id)
    plans = resolve_song_ad_plans(conn, sid)
    if not plans:
        return 0
    due_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    inserted = 0
    for plan in plans:
        progress = song_ad_progress(conn, plan, sid)
        if not progress["due"]:
            continue
        cycle = int(progress["cycle"])
        dedupe_key = f"broadcast-plan:{int(plan['plan_id'])}:song:{sid}:{cycle}"
        existing = conn.execute(
            "SELECT id FROM ad_break_items WHERE station_id=? AND dedupe_key=? "
            "AND status IN ('pending','playing','done') LIMIT 1",
            (sid, dedupe_key),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO ad_break_items "
            "(station_id, track_id, due_at, status, priority, dedupe_key) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (sid, int(plan["track_id"]), due_at, int(plan["priority"]), dedupe_key),
        )
        inserted += 1
    if inserted:
        conn.commit()
        with _CACHE_LOCK:
            _AD_CACHE.pop(sid, None)
    return inserted
