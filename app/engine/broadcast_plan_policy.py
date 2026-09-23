"""Station-local broadcast plan policy helpers shared by playout workers."""

from __future__ import annotations

import json
import threading
import time as monotonic_time
from datetime import date, datetime, time, timedelta
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
