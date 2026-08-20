from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import require_any_permission, require_permission
from app.db import get_connection, init_db
from app.services.dayparting import (
    DAYPART_SETTING_KEY,
    DAYPART_TIMEZONE_KEY,
    DAY_NAMES,
    DEFAULT_TIMEZONE,
    active_daypart,
    bpm_coverage,
    default_rules_for_station,
    load_station_dayparts,
    rule_payload,
    station_profile,
)


router = APIRouter()


class DaypartRuleInput(BaseModel):
    day: str = "Monday"
    name: str = Field(min_length=1, max_length=80)
    start: str
    end: str
    min_bpm: float = Field(ge=30, le=240)
    max_bpm: float = Field(ge=30, le=240)
    enabled: bool = True


class DaypartScheduleUpdate(BaseModel):
    enabled: bool = True
    timezone: str = DEFAULT_TIMEZONE
    rules: list[DaypartRuleInput] = Field(min_length=1, max_length=84)


def _parse_clock(value: str) -> int:
    token = str(value or "").strip()
    parts = token.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise HTTPException(status_code=422, detail=f"Invalid time '{token}'; expected HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HTTPException(status_code=422, detail=f"Invalid time '{token}'; expected HH:MM")
    return hour * 60 + minute


def _validate_rules(rules: list[DaypartRuleInput]) -> list[tuple[DaypartRuleInput, int, int, int]]:
    normalized: list[tuple[DaypartRuleInput, int, int, int]] = []
    occupied = {day: [0] * 1440 for day in range(7)}
    for rule in rules:
        day_token = str(rule.day or "").strip().lower()
        try:
            day_of_week = next(index for index, name in enumerate(DAY_NAMES) if name.lower() == day_token)
        except StopIteration:
            raise HTTPException(status_code=422, detail=f"Unknown weekday '{rule.day}'")
        start = _parse_clock(rule.start)
        end = _parse_clock(rule.end)
        if start == end:
            raise HTTPException(status_code=422, detail=f"{rule.name}: start and end cannot match")
        if float(rule.min_bpm) > float(rule.max_bpm):
            raise HTTPException(status_code=422, detail=f"{rule.name}: min_bpm exceeds max_bpm")
        if not rule.enabled:
            raise HTTPException(
                status_code=422,
                detail=f"{rule.name}: disabled rules would leave a scheduling gap; remove or replace it",
            )
        minutes = range(start, end) if start < end else (*range(start, 1440), *range(0, end))
        for minute in minutes:
            occupied[day_of_week][minute] += 1
        normalized.append((rule, day_of_week, start, end))
    for day_of_week, clock in occupied.items():
        overlap = sum(1 for count in clock if count > 1)
        gap = sum(1 for count in clock if count == 0)
        if overlap or gap:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{DAY_NAMES[day_of_week]} must cover every minute exactly once "
                    f"(gap={gap}, overlap={overlap})"
                ),
            )
    return normalized


def _payload(conn, station_id: int) -> dict:
    station_name, enabled, timezone_name, rules = load_station_dayparts(conn, station_id)
    if not station_name:
        raise HTTPException(status_code=404, detail="station not found")
    current = active_daypart(conn, station_id)
    serialized_rules = [rule_payload(rule) for rule in rules]
    return {
        "station_id": int(station_id),
        "station_name": station_name,
        "profile": station_profile(station_name),
        "enabled": enabled,
        "timezone": timezone_name,
        "current_program": rule_payload(current) if current else None,
        "rules": serialized_rules,
        "days": [
            {"day": day, "day_of_week": index, "rules": [rule for rule in serialized_rules if rule["day_of_week"] == index]}
            for index, day in enumerate(DAY_NAMES)
        ],
        "bpm_coverage": bpm_coverage(conn, station_id),
        "selection_policy": {
            "primary": "least-played tracks inside the current BPM range",
            "fallback": "unknown-BPM tracks, then any eligible music, so broadcast continuity wins",
        },
    }


@router.get("/api/dayparts")
def get_dayparts(
    station_id: int,
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        return _payload(conn, station_id)
    finally:
        conn.close()


@router.put("/api/dayparts/{station_id}")
def update_dayparts(
    station_id: int,
    payload: DaypartScheduleUpdate,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    try:
        ZoneInfo(str(payload.timezone or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="Unknown IANA timezone")
    normalized = _validate_rules(payload.rules)
    conn = get_connection()
    try:
        station = conn.execute("SELECT id FROM stations WHERE id=?", (int(station_id),)).fetchone()
        if station is None:
            raise HTTPException(status_code=404, detail="station not found")
        with conn:
            conn.execute("DELETE FROM daypart_rules WHERE station_id=?", (int(station_id),))
            positions = {day: 0 for day in range(7)}
            for rule, day_of_week, start, end in normalized:
                position = positions[day_of_week]
                positions[day_of_week] += 1
                conn.execute(
                    "INSERT INTO daypart_rules "
                    "(station_id, day_of_week, position, name, start_minute, end_minute, min_bpm, max_bpm, enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        int(station_id),
                        day_of_week,
                        position,
                        str(rule.name).strip(),
                        start,
                        end,
                        float(rule.min_bpm),
                        float(rule.max_bpm),
                    ),
                )
            conn.execute(
                "INSERT INTO station_settings(station_id, key, value, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (int(station_id), DAYPART_SETTING_KEY, "true" if payload.enabled else "false"),
            )
            conn.execute(
                "INSERT INTO station_settings(station_id, key, value, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (int(station_id), DAYPART_TIMEZONE_KEY, str(payload.timezone).strip()),
            )
        return _payload(conn, station_id)
    finally:
        conn.close()


@router.post("/api/dayparts/{station_id}/reset")
def reset_dayparts(
    station_id: int,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    try:
        station = conn.execute("SELECT name FROM stations WHERE id=?", (int(station_id),)).fetchone()
        if station is None:
            raise HTTPException(status_code=404, detail="station not found")
        if not default_rules_for_station(str(station["name"] or "")):
            raise HTTPException(status_code=400, detail="No RadioTEDU default profile for this station")
        with conn:
            conn.execute("DELETE FROM daypart_rules WHERE station_id=?", (int(station_id),))
            conn.execute(
                "INSERT INTO station_settings(station_id, key, value, updated_at) "
                "VALUES (?, ?, 'true', CURRENT_TIMESTAMP) "
                "ON CONFLICT(station_id, key) DO UPDATE SET value='true', updated_at=CURRENT_TIMESTAMP",
                (int(station_id), DAYPART_SETTING_KEY),
            )
            conn.execute(
                "INSERT INTO station_settings(station_id, key, value, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (int(station_id), DAYPART_TIMEZONE_KEY, DEFAULT_TIMEZONE),
            )
        return _payload(conn, station_id)
    finally:
        conn.close()
