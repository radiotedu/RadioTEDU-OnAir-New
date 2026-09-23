"""Validated multi-station programming plans and idempotent playout materialization."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import get_connection
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.schedule_repo import ScheduleRepository

logger = logging.getLogger(__name__)


def _plan_days(plan) -> set[int]:
    try:
        return {int(value) for value in json.loads(str(plan["weekdays_json"] or "[]"))}
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def _scheduled_slots(plan, anchor_day: date):
    """Yield timezone-aware slots for one local calendar day."""
    zone = ZoneInfo(str(plan["timezone"] or "Europe/Istanbul"))
    start_clock = time.fromisoformat(str(plan["local_start"]))
    end_clock = time.fromisoformat(str(plan["local_end"]))
    start = datetime.combine(anchor_day, start_clock, tzinfo=zone)
    repeat_minutes = max(0, int(plan["repeat_every_minutes"] or 0))
    if repeat_minutes <= 0:
        yield start
        return
    end = datetime.combine(anchor_day, end_clock, tzinfo=zone)
    if end <= start:
        end += timedelta(days=1)
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(minutes=repeat_minutes)


def _occurrence_key(plan_id: int, scheduled_utc: datetime) -> str:
    stamp = scheduled_utc.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    return f"broadcast-plan:{int(plan_id)}:{stamp}"


def cancel_future_occurrences(conn, plan_id: int) -> int:
    """Cancel pending future occurrences while leaving the current transition intact."""
    cutoff = (datetime.now(timezone.utc) + timedelta(minutes=1)).replace(tzinfo=None)
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM broadcast_plan_occurrences WHERE plan_id=? AND status='pending' AND scheduled_at>?",
        (int(plan_id), cutoff.isoformat(sep=" ", timespec="seconds")),
    )
    rows = list(cur.fetchall())
    for row in rows:
        if str(row["target_kind"]) == "ad":
            cur.execute(
                "UPDATE ad_break_items SET status='cancelled' WHERE id=? AND status='pending'",
                (int(row["target_item_id"]),),
            )
        elif str(row["target_kind"]) == "schedule":
            cur.execute(
                "UPDATE schedule_items SET status='cancelled' WHERE id=? AND status='pending'",
                (int(row["target_item_id"]),),
            )
        cur.execute(
            "UPDATE broadcast_plan_occurrences SET status='cancelled' WHERE id=? AND status='pending'",
            (int(row["id"]),),
        )
    conn.commit()
    return len(rows)


def _materialize_one(conn, plan, target, scheduled_local: datetime, now_utc: datetime) -> bool:
    plan_id = int(plan["id"])
    station_id = int(target["station_id"])
    track_id = int(target["track_id"])
    scheduled_utc = scheduled_local.astimezone(timezone.utc).replace(microsecond=0)
    if scheduled_utc < now_utc - timedelta(seconds=30):
        return False
    at_text = scheduled_utc.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    key = _occurrence_key(plan_id, scheduled_utc)
    occurrence = conn.execute(
        "SELECT * FROM broadcast_plan_occurrences WHERE plan_id=? AND station_id=? AND scheduled_at=?",
        (plan_id, station_id, at_text),
    ).fetchone()
    if occurrence is not None and str(occurrence["status"]) != "cancelled":
        return False

    kind = str(plan["plan_type"])
    if kind == "ad":
        target_kind = "ad"
        if occurrence is not None:
            item_id = int(occurrence["target_item_id"])
            cur = conn.cursor()
            cur.execute(
                "UPDATE ad_break_items SET track_id=?, due_at=?, status='pending', priority=?, "
                "started_at=NULL, finished_at=NULL, dedupe_key=? WHERE id=? AND status='cancelled'",
                (track_id, at_text, int(plan["priority"]), key, item_id),
            )
            if cur.rowcount == 0:
                item_id = AdBreakRepository(conn).enqueue(
                    station_id, track_id, at_text, int(plan["priority"]), key
                )
        else:
            item_id = AdBreakRepository(conn).enqueue(
                station_id, track_id, at_text, int(plan["priority"]), key
            )
    else:
        target_kind = "schedule"
        window_minutes = max(1, int(plan["play_window_minutes"] or 15))
        window_end = (scheduled_utc + timedelta(minutes=window_minutes)).replace(
            tzinfo=None
        ).isoformat(sep=" ", timespec="seconds")
        if occurrence is not None:
            item_id = int(occurrence["target_item_id"])
            cur = conn.cursor()
            cur.execute(
                "UPDATE schedule_items SET track_id=?, play_at=?, window_end=?, "
                "event_name=?, status='pending' WHERE id=? AND status='cancelled'",
                (track_id, at_text, window_end, key, item_id),
            )
            if cur.rowcount == 0:
                item_id = ScheduleRepository(conn).enqueue(
                    station_id, track_id, at_text, window_end, event_name=key
                )
        else:
            existing = conn.execute(
                "SELECT id, status FROM schedule_items WHERE station_id=? AND event_name=? LIMIT 1",
                (station_id, key),
            ).fetchone()
            if existing is not None:
                item_id = int(existing["id"])
                if str(existing["status"]) == "cancelled":
                    conn.execute(
                        "UPDATE schedule_items SET track_id=?, play_at=?, window_end=?, status='pending' WHERE id=?",
                        (track_id, at_text, window_end, item_id),
                    )
                    conn.commit()
            else:
                try:
                    item_id = ScheduleRepository(conn).enqueue(
                        station_id, track_id, at_text, window_end, event_name=key
                    )
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT id FROM schedule_items WHERE station_id=? AND event_name=? LIMIT 1",
                        (station_id, key),
                    ).fetchone()
                    if existing is None:
                        raise
                    item_id = int(existing["id"])

    if occurrence is None:
        conn.execute(
            "INSERT INTO broadcast_plan_occurrences "
            "(plan_id, station_id, scheduled_at, target_kind, target_item_id, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (plan_id, station_id, at_text, target_kind, item_id),
        )
    else:
        conn.execute(
            "UPDATE broadcast_plan_occurrences SET target_kind=?, target_item_id=?, status='pending' WHERE id=?",
            (target_kind, item_id, int(occurrence["id"])),
        )
    conn.commit()
    return True


def materialize_broadcast_plans(conn=None, horizon_days: int = 14) -> dict:
    """Queue near-term ad and recorded-program occurrences idempotently."""
    owns_connection = conn is None
    connection = conn or get_connection()
    made = 0
    try:
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        today_local = datetime.now(ZoneInfo("Europe/Istanbul")).date()
        final_day = today_local + timedelta(days=max(1, min(int(horizon_days), 60)))
        plans = connection.execute(
            "SELECT * FROM broadcast_plans WHERE enabled=1 AND plan_type IN ('ad','recorded_program') "
            "AND ends_on>=? AND starts_on<=? ORDER BY priority DESC, id",
            (today_local.isoformat(), final_day.isoformat()),
        ).fetchall()
        for plan in plans:
            targets = connection.execute(
                "SELECT station_id, track_id FROM broadcast_plan_targets "
                "WHERE plan_id=? AND enabled=1 ORDER BY station_id",
                (int(plan["id"]),),
            ).fetchall()
            try:
                start_day = max(today_local - timedelta(days=1), date.fromisoformat(str(plan["starts_on"])))
                end_day = min(final_day, date.fromisoformat(str(plan["ends_on"])))
                weekdays = _plan_days(plan)
                day = start_day
                while day <= end_day:
                    if day.isoweekday() in weekdays:
                        for local_slot in _scheduled_slots(plan, day):
                            for target in targets:
                                if _materialize_one(connection, plan, target, local_slot, now_utc):
                                    made += 1
                    day += timedelta(days=1)
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.exception("Invalid broadcast plan schedule id=%s", plan["id"])
        return {"materialized": made, "plans_checked": len(plans)}
    finally:
        if owns_connection:
            connection.close()


class BroadcastPlannerScheduler:
    """Small watchdog that extends a rolling 14-day playout horizon."""

    def __init__(self, interval_seconds: int = 60):
        self.interval_seconds = max(15, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="broadcast-planner-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = materialize_broadcast_plans()
                if result["materialized"]:
                    logger.info("Broadcast planner queued %s occurrences", result["materialized"])
            except Exception:
                logger.exception("Broadcast planner could not materialize its rolling schedule")
            self._stop.wait(self.interval_seconds)
