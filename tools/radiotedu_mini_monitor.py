"""Lightweight, read-only Windows monitor for the RadioTEDU playout sources.

Run with pythonw.exe.  The window reports source health from station worker
heartbeats; it does not equate a connected source with listener delivery.
"""

from __future__ import annotations

import ctypes
import http.client
import json
import os
import queue
import sqlite3
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo


MONITOR_API_HOST = "127.0.0.1"
MONITOR_API_PORT = 18110
MONITOR_API_PATH = "/api/monitor/snapshot"
STATE_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir" / "State" / "StationWorkers"
DATABASE = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir" / "cleanroom.db"
_LOCAL_DATA_LOCK = threading.Lock()
_LOCAL_DATA_CACHE: tuple[dict[int, dict], dict] | None = None
_LOCAL_DATA_CACHE_AT = 0.0
_LOCAL_DATA_CACHE_TTL_SECONDS = 30.0
_LOCAL_DATA_ERROR = ""
STATIONS = (
    (1, "Classical", "/classic"),
    (2, "Lo-Fi", "/lofi"),
    (4, "RadioTEDU Pop", "/radio"),
    (5, "Jazz", "/cazz"),
    (8, "Rock", "/rock"),
    (9, "Energize", "/energize"),
    (10, "Situation Room", "/situation"),
    (11, "Min Character", "/maincharacter"),
)
REFRESH_SECONDS = 12
LISTENER_HOST = "stream.radiotedu.com"
LISTENER_PORT = 11154
_LISTENER_LOCK = threading.Lock()
_LISTENER_RESULTS: dict[int, dict] = {}


def _probe_listeners() -> None:
    """Spread 512-byte listener canaries across stations to keep origin load low."""
    order = (4, 11, 5, 9, 1, 2, 8, 10)
    mounts = {sid: mount for sid, _, mount in STATIONS}
    while True:
        for station_id in order:
            conn = http.client.HTTPConnection(LISTENER_HOST, LISTENER_PORT, timeout=3)
            ok = False
            detail = ""
            try:
                conn.request("GET", mounts[station_id], headers={"Icy-MetaData": "0"})
                response = conn.getresponse()
                content_type = (response.getheader("Content-Type") or "").lower()
                payload = response.read(512)
                ok = response.status == 200 and (
                    content_type.startswith("audio/") or "ogg" in content_type
                ) and bool(payload)
                detail = f"HTTP {response.status}, {content_type}, {len(payload)} bytes"
            except (OSError, http.client.HTTPException) as exc:
                detail = f"{type(exc).__name__}: {exc}"
            finally:
                conn.close()
            with _LISTENER_LOCK:
                previous = _LISTENER_RESULTS.get(station_id) or {}
                _LISTENER_RESULTS[station_id] = {
                    "ok": ok, "at": time.time(), "detail": detail,
                    "failures": 0 if ok else int(previous.get("failures") or 0) + 1,
                }
            time.sleep(19)


def _single_instance() -> bool:
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, True, "Local\\RadioTEDU.OnAir.MiniMonitor")
    # Keep the handle alive for the lifetime of the process.
    globals()["_MUTEX_HANDLE"] = handle
    return bool(handle) and kernel32.GetLastError() != 183


def _snapshot() -> dict:
    connection = http.client.HTTPConnection(
        MONITOR_API_HOST, MONITOR_API_PORT, timeout=15.0
    )
    try:
        connection.request("GET", MONITOR_API_PATH, headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read(262144)
        if response.status != 200:
            raise OSError(f"Health API HTTP {response.status}")
        snapshot = json.loads(payload.decode("utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("Health API response is not an object")
    finally:
        connection.close()

    observed_at = (
        (snapshot.get("freshness") or {}).get("runtime_observed_at")
        or snapshot.get("generated_at")
        or time.time()
    )
    stations = {
        int(item["station_id"]): item
        for item in snapshot.get("stations", [])
        if isinstance(item, dict) and item.get("station_id") is not None
    }
    needs_local = not isinstance(snapshot.get("ad_monitor"), dict) or any(
        not isinstance(item.get("playout_monitor"), dict)
        or item["playout_monitor"].get("state") == "unavailable"
        for item in stations.values()
    )
    local_playout, local_ad = _read_local_monitor_data() if needs_local else ({}, None)
    heartbeats: dict[int, dict] = {}
    playing: dict[int, dict] = {}
    for station_id, _, _ in STATIONS:
        station = stations.get(station_id)
        if station is None:
            continue
        runtime = station.get("runtime") or {}
        station_health = str(station.get("health") or "unknown")
        station_observed_at = observed_at
        external_live = False
        if station_id == 11:
            external = _read_maincharacter_runtime()
            if external:
                runtime = {**runtime, **external["runtime"]}
                station_observed_at = external["updated_epoch"]
                mount = runtime.get("icecast_mount_health") or {}
                external_live = bool(
                    runtime.get("program_running")
                    and runtime.get("output_feed_active")
                    and runtime.get("program_pcm_stalled") is not True
                    and mount.get("mount_healthy") is True
                )
                station_health = (
                    "healthy"
                    if external_live
                    else "degraded"
                )
        playout = station.get("playout_monitor")
        if not isinstance(playout, dict) or playout.get("state") == "unavailable":
            playout = local_playout.get(station_id) or {"state": "unavailable"}
        public_status = station.get("public_status")
        if not public_status:
            public_status = "live" if station.get("health") == "healthy" else "unknown"
        is_live = public_status == "live" or external_live
        current = (
            station.get("now_playing")
            if public_status == "live"
            else (station.get("preserved_item") if external_live else {})
        )
        last_item = station.get("preserved_item") if not is_live else {}
        if isinstance(current, str):
            current = {"title": current}
        if not isinstance(current, dict):
            current = {}
        if isinstance(last_item, str):
            last_item = {"title": last_item}
        if not isinstance(last_item, dict):
            last_item = {}
        if is_live and not current and playout.get("current_title"):
            current = {
                "title": playout.get("current_title"),
                "artist": playout.get("current_artist"),
                "track_type": playout.get("current_track_type"),
            }
        playing[station_id] = {
            **playout,
            "is_live": is_live,
            "station_health": station_health,
            "active_show_name": str(station.get("active_show_name") or ""),
            "program_running": bool(runtime.get("program_running")),
            "title": str(
                runtime.get("active_stream_title")
                or current.get("title")
                or current.get("name")
                or ""
            ),
            "artist": str(
                runtime.get("active_stream_artist")
                or current.get("artist")
                or ""
            ),
            "track_type": str(
                runtime.get("active_track_type")
                or current.get("track_type")
                or ""
            ),
            "last_title": str(last_item.get("title") or last_item.get("name") or ""),
            "last_artist": str(last_item.get("artist") or ""),
        }
        heartbeats[station_id] = {
            "updated_epoch": station_observed_at,
            "running": bool(
                runtime.get("alive")
                or runtime.get("running")
                or runtime.get("program_running")
            ),
            "runtime_status": runtime,
            "station_health": station_health,
        }
    with _LISTENER_LOCK:
        listeners = dict(_LISTENER_RESULTS)
    return {
        "heartbeats": heartbeats,
        "playing": playing,
        "listeners": listeners,
        "ad": (
            snapshot.get("ad_monitor")
            if isinstance(snapshot.get("ad_monitor"), dict)
            and snapshot["ad_monitor"].get("state") != "unavailable"
            else (local_ad or {"state": "unsupported"})
        ),
        "error": None,
        "at": time.time(),
    }


def _read_maincharacter_runtime() -> dict | None:
    """Use the standalone station-11 heartbeat missing from the legacy API."""
    try:
        payload = json.loads(
            (STATE_ROOT / "station-11.heartbeat.json").read_text(encoding="utf-8")
        )
        updated_epoch = float(payload.get("updated_epoch") or 0)
        age = time.time() - updated_epoch
        tick_age_value = payload.get("scheduler_tick_age_seconds")
        tick_age = 999999.0 if tick_age_value is None else float(tick_age_value)
        runtime = payload.get("runtime_status")
        if (
            int(payload.get("station_id") or 0) != 11
            or not payload.get("running")
            or payload.get("scheduler_stalled")
            or age < 0
            or age > 20
            or tick_age < 0
            or tick_age > 5
            or not isinstance(runtime, dict)
        ):
            return None
        safe = {
            key: runtime.get(key)
            for key in (
                "state",
                "alive",
                "running",
                "program_running",
                "output_feed_active",
                "program_pcm_stalled",
                "producer_eof",
                "active_stream_title",
                "active_stream_artist",
                "active_track_type",
            )
            if key in runtime
        }
        mount = runtime.get("icecast_mount_health")
        if isinstance(mount, dict):
            safe["icecast_mount_health"] = {
                key: mount.get(key)
                for key in (
                    "process_running",
                    "mount_healthy",
                    "consecutive_probe_failures",
                    "last_network_write_age_seconds",
                    "encoded_bytes_sent",
                )
                if key in mount
            }
        for source_key, safe_key in (
            ("branch_health", "branches"),
            ("delivery_health", "deliveries"),
            ("required_outputs", "required_outputs"),
        ):
            values = runtime.get(source_key)
            if isinstance(values, dict):
                safe[safe_key] = {
                    str(name)[:80]: bool(value)
                    for name, value in values.items()
                    if isinstance(name, str)
                }
        recovery = runtime.get("recovery")
        if isinstance(recovery, dict):
            safe["recovery"] = {
                key: recovery.get(key)
                for key in ("state", "attempt_count", "error_code", "retry_in_seconds")
                if key in recovery
            }
        return {"runtime": safe, "updated_epoch": updated_epoch}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _monitor_plan_window_active(plan: sqlite3.Row) -> bool:
    zone = ZoneInfo(str(plan["timezone"] or "Europe/Istanbul"))
    current = datetime.now(timezone.utc).astimezone(zone)
    starts_on = date.fromisoformat(str(plan["starts_on"]))
    ends_on = date.fromisoformat(str(plan["ends_on"]))
    start_time = day_time.fromisoformat(str(plan["local_start"]))
    end_time = day_time.fromisoformat(str(plan["local_end"]))
    weekdays = {int(value) for value in json.loads(str(plan["weekdays_json"] or "[]"))}
    for anchor_day in (current.date(), current.date() - timedelta(days=1)):
        if not starts_on <= anchor_day <= ends_on or anchor_day.isoweekday() not in weekdays:
            continue
        anchor_start = datetime.combine(anchor_day, start_time, tzinfo=zone)
        anchor_end = datetime.combine(anchor_day, end_time, tzinfo=zone)
        if anchor_end <= anchor_start:
            anchor_end += timedelta(days=1)
        if anchor_start <= current < anchor_end:
            return True
    return False


def _read_local_monitor_data() -> tuple[dict[int, dict], dict]:
    """Read compact, read-only program/ad state when the installed API is old."""
    global _LOCAL_DATA_CACHE, _LOCAL_DATA_CACHE_AT, _LOCAL_DATA_ERROR
    now = time.monotonic()
    with _LOCAL_DATA_LOCK:
        if (
            _LOCAL_DATA_CACHE is not None
            and now - _LOCAL_DATA_CACHE_AT < _LOCAL_DATA_CACHE_TTL_SECONDS
        ):
            return (
                {key: dict(value) for key, value in _LOCAL_DATA_CACHE[0].items()},
                dict(_LOCAL_DATA_CACHE[1]),
            )

        playout: dict[int, dict] = {}
        ad: dict = {"state": "unavailable"}
        conn = None
        stage = "connect"
        try:
            _LOCAL_DATA_ERROR = ""
            conn = sqlite3.connect(
                f"file:{DATABASE.as_posix()}?mode=ro", uri=True, timeout=0.7
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=700")
            stage = "eligible_music"
            station_ids = tuple(station_id for station_id, _, _ in STATIONS)
            placeholders = ",".join("?" for _ in station_ids)
            eligible = {
                int(row["station_id"]): int(row["amount"] or 0)
                for row in conn.execute(
                    "SELECT station_id, COUNT(*) AS amount FROM tracks "
                    f"WHERE station_id IN ({placeholders}) AND is_active=1 "
                    "AND LOWER(COALESCE(track_type,'music'))='music' "
                    "AND COALESCE(file_path,'')<>'' GROUP BY station_id",
                    station_ids,
                )
            }
            pending_music = {
                int(row["station_id"]): int(row["amount"] or 0)
                for row in conn.execute(
                    "SELECT q.station_id, COUNT(*) AS amount FROM queue_items q "
                    "JOIN tracks t ON t.id=q.track_id "
                    f"WHERE q.station_id IN ({placeholders}) AND q.status='pending' "
                    "AND t.is_active=1 AND LOWER(COALESCE(t.track_type,'music'))='music' "
                    "AND COALESCE(t.file_path,'')<>'' "
                    "GROUP BY q.station_id",
                    station_ids,
                )
            }
            stage = "queue_items"
            for station_id in station_ids:
                playout[station_id] = {
                    "state": "ready",
                    "eligible_music_count": eligible.get(station_id, 0),
                    "pending_count": pending_music.get(station_id, 0),
                    "program_ready": bool(
                        eligible.get(station_id, 0) or pending_music.get(station_id, 0)
                    ),
                    "next_title": "",
                    "next_artist": "",
                    "current_title": "",
                    "current_artist": "",
                    "current_track_type": "",
                }

            stage = "queue_details"
            queue_rows = conn.execute(
                "SELECT q.station_id, q.status, q.position, q.id, "
                "COALESCE(t.title,'') AS title, COALESCE(t.artist,'') AS artist, "
                "COALESCE(t.track_type,'music') AS track_type "
                "FROM queue_items q JOIN tracks t ON t.id=q.track_id "
                f"WHERE q.station_id IN ({placeholders}) "
                "AND q.status IN ('playing','pending') AND t.is_active=1 "
                "AND COALESCE(t.file_path,'')<>'' "
                "ORDER BY q.station_id, CASE q.status WHEN 'playing' THEN 0 ELSE 1 END, "
                "q.position, q.id",
                station_ids,
            )
            for row in queue_rows:
                station_id = int(row["station_id"])
                target = playout[station_id]
                if row["status"] == "playing" and not target["current_title"]:
                    target["current_title"] = str(row["title"] or "")
                    target["current_artist"] = str(row["artist"] or "")
                    target["current_track_type"] = str(row["track_type"] or "music")
                elif row["status"] == "pending" and not target["next_title"]:
                    target["next_title"] = str(row["title"] or "")
                    target["next_artist"] = str(row["artist"] or "")
                    target["next_track_type"] = str(row["track_type"] or "music")

            stage = "ad_playing"
            playing_ad = conn.execute(
                "SELECT a.id, a.status, a.due_at, COALESCE(t.title,'') AS title, "
                "COALESCE(t.artist,'') AS artist, "
                "CASE WHEN COALESCE(t.file_path,'')<>'' THEN 1 ELSE 0 END AS media_ready "
                "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
                "WHERE a.station_id=4 AND a.status='playing' "
                "ORDER BY a.started_at, a.id LIMIT 1"
            ).fetchone()
            if playing_ad:
                ad = {"state": "playing_unverified", **dict(playing_ad)}
            else:
                stage = "ad_pending"
                pending_ad = conn.execute(
                    "SELECT a.id, a.status, a.due_at, COALESCE(t.title,'') AS title, "
                    "COALESCE(t.artist,'') AS artist, "
                    "CASE WHEN COALESCE(t.file_path,'')<>'' THEN 1 ELSE 0 END AS media_ready "
                    "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
                    "WHERE a.station_id=4 AND a.status='pending' "
                    "ORDER BY datetime(a.due_at), a.priority DESC, a.id LIMIT 1"
                ).fetchone()
                stage = "ad_plans"
                plans = conn.execute(
                    "SELECT p.id,p.name,p.enabled,p.starts_on,p.ends_on,p.weekdays_json, "
                    "p.local_start,p.local_end,p.timezone,p.repeat_every_songs,p.priority, "
                    "p.created_at,t.enabled AS target_enabled,COALESCE(tr.is_active,0) AS track_active, "
                    "CASE WHEN COALESCE(tr.file_path,'')<>'' THEN 1 ELSE 0 END AS media_ready "
                    "FROM broadcast_plans p "
                    "LEFT JOIN broadcast_plan_targets t ON t.plan_id=p.id AND t.station_id=4 "
                    "LEFT JOIN tracks tr ON tr.id=t.track_id "
                    "WHERE p.plan_type='ad' AND p.cadence_mode='songs' "
                    "AND EXISTS(SELECT 1 FROM broadcast_plan_targets x "
                    "WHERE x.plan_id=p.id AND x.station_id=4) "
                    "ORDER BY p.priority DESC,p.id DESC"
                ).fetchall()
                active = []
                inactive_reason = ""
                stage = "ad_plan_window"
                for plan in plans:
                    if not bool(plan["enabled"]):
                        inactive_reason = "plan_disabled"
                        continue
                    if not bool(plan["target_enabled"]):
                        inactive_reason = "target_disabled"
                        continue
                    try:
                        if not _monitor_plan_window_active(plan):
                            inactive_reason = "outside_schedule_window"
                            continue
                    except Exception:
                        inactive_reason = "invalid_plan"
                        continue
                    if not bool(plan["track_active"]) or not bool(plan["media_ready"]):
                        inactive_reason = "ad_track_inactive_or_missing"
                        continue
                    active.append(plan)

                if pending_ad:
                    ad = {key: value for key, value in dict(pending_ad).items() if key != "media_ready"}
                    ad["state"] = "pending" if bool(pending_ad["media_ready"]) else "pending_media_missing"
                elif not active:
                    ad = (
                        {"state": "inactive", "reason": inactive_reason or "no_eligible_target"}
                        if plans else {"state": "no_plan"}
                    )
                else:
                    plan = active[0]
                    interval = max(1, int(plan["repeat_every_songs"] or 10))
                    stage = "ad_music_count"
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS amount FROM queue_items q JOIN tracks t ON t.id=q.track_id "
                        "WHERE q.station_id=4 AND t.station_id=4 AND q.status='done' "
                        "AND LOWER(COALESCE(t.track_type,'music'))='music' "
                        "AND q.finished_at IS NOT NULL AND datetime(q.finished_at)>=datetime(?)",
                        (str(plan["created_at"] or "1970-01-01 00:00:00"),),
                    ).fetchone()
                    music_count = int((count_row["amount"] if count_row else 0) or 0)
                    completed_cycles = music_count // interval
                    stage = "ad_cycle_status"
                    statuses = conn.execute(
                        "SELECT dedupe_key,status FROM ad_break_items WHERE station_id=4 "
                        "AND dedupe_key LIKE ? ORDER BY id DESC",
                        (f"broadcast-plan:{int(plan['id'])}:song:4:%",),
                    ).fetchall()
                    status_by_cycle: dict[int, str] = {}
                    for row in statuses:
                        try:
                            cycle = int(str(row["dedupe_key"]).rsplit(":",1)[1])
                        except (IndexError, TypeError, ValueError):
                            continue
                        status_by_cycle.setdefault(cycle, str(row["status"] or ""))
                    due_cycle = next(
                        (cycle for cycle in range(1, completed_cycles + 1)
                         if status_by_cycle.get(cycle) != "done"),
                        None,
                    )
                    if due_cycle is not None:
                        ad = {
                            "state": "unmaterialized",
                            "reason": "due_without_pending_row",
                            "name": str(plan["name"] or ""),
                            "interval": interval,
                            "remaining": 0,
                            "cycle": due_cycle,
                        }
                    else:
                        remainder = music_count % interval
                        ad = {
                            "state": "countdown",
                            "name": str(plan["name"] or ""),
                            "interval": interval,
                            "remaining": interval - remainder if remainder else interval,
                        }
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            detail = str(exc).replace(str(DATABASE), "[database]").splitlines()[0][:120]
            _LOCAL_DATA_ERROR = f"{stage}:{type(exc).__name__}:{detail}"
            playout = {station_id: {"state": "unavailable"} for station_id, _, _ in STATIONS}
            ad = {"state": "unavailable"}
        finally:
            if conn is not None:
                conn.close()

        _LOCAL_DATA_CACHE = (playout, ad)
        _LOCAL_DATA_CACHE_AT = now
        return (
            {key: dict(value) for key, value in playout.items()},
            dict(ad),
        )


def _classify(heartbeat: dict, prior: dict | None, listener: dict | None) -> tuple[str, str]:
    if not heartbeat:
        return "SAĞLIK VERİSİ YOK", "warn"
    age = time.time() - float(heartbeat.get("updated_epoch") or 0)
    if age > 20:
        return "SAĞLIK VERİSİ ESKİ", "warn"
    if not heartbeat.get("running"):
        return "DURDU", "bad"
    runtime = heartbeat.get("runtime_status") or {}
    station_health = str(heartbeat.get("station_health") or "unknown").lower()
    health = runtime.get("icecast_mount_health") or {}
    if runtime.get("program_pcm_stalled") is True:
        return "PROGRAM SESİ DURDU", "bad"
    if runtime.get("producer_eof") is True:
        return "PROGRAM KAYNAĞI BİTTİ", "bad"
    if not runtime.get("program_running"):
        if runtime.get("output_feed_active"):
            return "PROGRAM İŞÇİSİ DURDU", "bad"
        return "PROGRAM HAZIR DEĞİL", "bad"
    if station_health == "unavailable":
        return "DİNLEYİCİ AKIŞI YOK", "bad"
    if runtime.get("output_feed_active") is False:
        return "ÇIKIŞ SESİ DURDU", "bad"
    deliveries = runtime.get("deliveries") or runtime.get("branches") or {}
    required = runtime.get("required_outputs") or {}
    required_names = [str(name) for name, value in required.items() if value]
    if any(deliveries.get(name) is False for name in required_names):
        return "GEREKLİ ÇIKIŞ KESİK", "bad"
    if not health:
        if station_health == "degraded":
            return "SAĞLIK BOZUK", "warn"
        if listener and time.time() - float(listener.get("at") or 0) < 300:
            if listener.get("ok"):
                return "DİNLEYİCİ CANARY OK", "ok"
            if int(listener.get("failures") or 0) >= 2:
                return "DİNLEYİCİ KESİK", "bad"
            return "DİNLEYİCİ?", "warn"
        if station_health == "healthy":
            return "KAYNAK SAĞLIKLI · DİNLEYİCİ BEKLENİYOR", "warn"
        return "SAĞLIK TELEMETRİSİ EKSİK", "warn"
    write_age = health.get("last_network_write_age_seconds")
    if write_age is None or float(write_age) > 15 or not health.get("process_running"):
        return "KAYNAK KESİK", "bad"
    if health.get("mount_healthy") is False:
        return "DİNLEYİCİ ÇIKIŞI KESİK", "bad"
    recovery = runtime.get("recovery") or {}
    if recovery.get("state") not in (None, "idle", "healthy"):
        return "TOPARLANIYOR", "warn"
    if prior and int(health.get("encoded_bytes_sent") or 0) <= int(prior.get("bytes") or 0):
        if time.time() - float(prior.get("at") or 0) >= 10:
            return "VERİ DURDU", "bad"
    if listener and time.time() - float(listener.get("at") or 0) < 300:
        if listener.get("ok"):
            return "DİNLENEBİLİR", "ok"
        if int(listener.get("failures") or 0) >= 2:
            return "DİNLEYİCİ KESİK", "bad"
        return "DİNLEYİCİ?", "warn"
    return "KAYNAK AKIYOR", "warn"


class Monitor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RadioTEDU · Yayın Durumu")
        self.geometry("590x820+30+50")
        self.minsize(540, 760)
        self.configure(bg="#101923")
        self.attributes("-topmost", True)
        self.resizable(True, True)
        self._result_queue: queue.Queue[dict] = queue.Queue(maxsize=1)
        self._busy = False
        self._prior: dict[int, dict] = {}
        self._build()
        threading.Thread(target=_probe_listeners, daemon=True).start()
        self.after(200, self._schedule)
        self.after(500, self._drain)

    def _build(self) -> None:
        bg, panel, ink, muted = "#101923", "#182532", "#F4F7FB", "#A9BBC8"
        title_font = tkfont.Font(family="Segoe UI", size=16, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        small_font = tkfont.Font(family="Segoe UI", size=9)
        header = tk.Frame(self, bg=bg)
        header.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(header, text="RadioTEDU", font=title_font, bg=bg, fg=ink).pack(side="left")
        self.summary = tk.Label(header, text="Bağlanıyor…", font=small_font, bg=bg, fg=muted)
        self.summary.pack(side="right", pady=(5, 0))
        tk.Label(self, text="Kaynak işçileri · dinleyici teslimi ayrıca izlenir", font=small_font,
                 bg=bg, fg=muted, anchor="w").pack(fill="x", padx=17, pady=(0, 9))

        self.rows: dict[int, tuple[tk.Label, tk.Label]] = {}
        for sid, name, mount in STATIONS:
            frame = tk.Frame(self, bg=panel, height=82)
            frame.pack(fill="x", padx=12, pady=2)
            frame.pack_propagate(False)
            upper = tk.Frame(frame, bg=panel)
            upper.pack(fill="x", padx=10, pady=(6, 1))
            tk.Label(upper, text=name, font=body_font, bg=panel, fg=ink, anchor="w").pack(side="left")
            tk.Label(upper, text=mount, font=small_font, bg=panel, fg=muted).pack(side="left", padx=7)
            state = tk.Label(upper, text="…", font=small_font, bg=panel, fg=muted)
            state.pack(side="right")
            song = tk.Label(frame, text="Parça bilgisi bekleniyor", font=small_font,
                            bg=panel, fg=muted, anchor="w")
            song.pack(fill="x", padx=10, pady=(0, 1))
            next_item = tk.Label(frame, text="Sıradaki: okunuyor…", font=small_font,
                                 bg=panel, fg=muted, anchor="w")
            next_item.pack(fill="x", padx=10)
            inventory = tk.Label(frame, text="Otomatik uygun: — · kuyruk: —", font=small_font,
                                 bg=panel, fg=muted, anchor="w")
            inventory.pack(fill="x", padx=10)
            self.rows[sid] = (state, song, next_item, inventory)

        ad_frame = tk.Frame(self, bg="#243347")
        ad_frame.pack(fill="x", padx=12, pady=(12, 5))
        tk.Label(ad_frame, text="RADIO / REKLAM", font=small_font, bg="#243347",
                 fg="#93BCF5").pack(anchor="w", padx=10, pady=(7, 0))
        self.ad_text = tk.Label(ad_frame, text="Plan okunuyor…", font=body_font,
                                bg="#243347", fg=ink, anchor="w")
        self.ad_text.pack(fill="x", padx=10, pady=(1, 8))
        footer = tk.Frame(self, bg=bg)
        footer.pack(fill="x", padx=16, pady=(5, 8))
        self.updated = tk.Label(footer, text="", font=small_font, bg=bg, fg=muted)
        self.updated.pack(side="left")
        tk.Button(footer, text="Yenile", command=self._request_refresh, bg="#263B51", fg=ink,
                  activebackground="#39536E", activeforeground=ink, relief="flat",
                  padx=9, pady=3).pack(side="right")

    def _schedule(self) -> None:
        self._request_refresh()
        self.after(REFRESH_SECONDS * 1000, self._schedule)

    def _request_refresh(self) -> None:
        if not self._busy:
            self._busy = True
            threading.Thread(target=self._collect, daemon=True).start()

    def _collect(self) -> None:
        try:
            data = _snapshot()
        except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
            data = {"heartbeats": {}, "playing": {}, "listeners": {},
                    "ad": {"state": "unavailable"},
                    "error": f"Yerel sağlık API'si okunamadı: {type(exc).__name__}",
                    "at": time.time()}
        try:
            self._result_queue.put_nowait(data)
        except queue.Full:
            pass

    def _drain(self) -> None:
        try:
            data = self._result_queue.get_nowait()
        except queue.Empty:
            self.after(500, self._drain)
            return
        try:
            self._render(data)
        except Exception as exc:
            self.summary.configure(text="GÖRÜNÜM HATASI", fg="#FF7F82")
            self.updated.configure(text=f"Ekran güncellenemedi · {type(exc).__name__}")
        finally:
            self._busy = False
            self.after(500, self._drain)

    def _render(self, data: dict) -> None:
        counts = {"ok": 0, "warn": 0, "bad": 0}
        palette = {"ok": "#6EE7A5", "warn": "#F8C66B", "bad": "#FF7F82"}
        for sid, _, _ in STATIONS:
            heartbeat = data["heartbeats"].get(sid) or {}
            state, level = _classify(heartbeat, self._prior.get(sid),
                                     data["listeners"].get(sid))
            counts[level] += 1
            state_label, song_label, next_label, inventory_label = self.rows[sid]
            playout = data["playing"].get(sid) or {}
            if playout.get("station_health") == "degraded" and level == "ok":
                level = "warn"
                counts["ok"] -= 1
                counts["warn"] += 1
            state_label.configure(text=state, fg=palette[level])
            if playout.get("title") and playout.get("is_live"):
                artist = str(playout.get("artist") or "").strip()
                title = str(playout.get("title") or "").strip()
                song = f"Şimdi: {artist} · {title}" if artist else f"Şimdi: {title}"
            elif playout.get("active_show_name") and playout.get("program_running"):
                song = f"Yayında: {playout['active_show_name']} · parça bilgisi bekleniyor"
            elif playout.get("program_running"):
                song = "Yayın programı çalışıyor · içerik etiketi bekleniyor"
            elif playout.get("program_ready"):
                song = (
                    "Program kuyruğu hazır · "
                    f"{int(playout.get('eligible_music_count') or 0)} uygun müzik"
                )
            elif playout.get("last_title"):
                last = f"{playout.get('last_artist')} · {playout.get('last_title')}".strip(" ·")
                song = f"Son çalan: {last} · yayın dışı"
            elif state in {"PROGRAM HAZIR DEĞİL", "PROGRAM SESİ DURDU", "PROGRAM KAYNAĞI BİTTİ"}:
                song = "Yayın kaynağı toparlanıyor"
            else:
                song = "Program içeriği sağlık API'sinden doğrulanamadı"
            song_label.configure(text=(song[:82] + "…") if len(song) > 83 else song)
            next_title = str(playout.get("next_title") or "").strip()
            next_artist = str(playout.get("next_artist") or "").strip()
            next_text = f"{next_artist} · {next_title}" if next_artist else next_title
            if next_text:
                next_text = f"Sıradaki: {next_text}"
            else:
                next_text = "Sıradaki kuyruk öğesi yok"
            next_label.configure(text=(next_text[:82] + "…") if len(next_text) > 83 else next_text)
            inventory_label.configure(
                text=(
                    "Program envanteri okunamadı"
                    if playout.get("state") == "unavailable"
                    else (
                        f"Otomatik uygun müzik: {int(playout.get('eligible_music_count') or 0)}"
                        f" · sıradaki öğeler: {int(playout.get('pending_count') or 0)}"
                    )
                )
            )
            health = (heartbeat.get("runtime_status") or {}).get("icecast_mount_health") or {}
            self._prior[sid] = {"bytes": health.get("encoded_bytes_sent") or 0, "at": data["at"]}

        checked = sum(
            1 for item in data["listeners"].values()
            if data["at"] - float(item.get("at") or 0) < 300
        )
        self.summary.configure(text=f"{counts['ok']}/{checked} doğrulandı · {counts['bad']} kesik")
        self.title(f"RadioTEDU · {counts['ok']}/{checked} dinlenebilir")
        ad = data.get("ad")
        if ad:
            if ad.get("state") == "playing":
                item = " · ".join(part for part in (str(ad.get("artist") or "").strip(), str(ad.get("title") or "").strip()) if part)
                text = f"Reklam yayında{f' · {item}' if item else ''}"
            elif ad.get("state") == "pending":
                item = " · ".join(part for part in (str(ad.get("artist") or "").strip(), str(ad.get("title") or "").strip()) if part)
                text = f"Reklam kuyruğa alınmış{f' · {item}' if item else ''}"
            elif ad.get("state") == "pending_media_missing":
                text = "Reklam kuyruğunda, ancak ses dosyası yolu eksik"
            elif ad.get("state") == "inactive":
                reasons = {
                    "plan_disabled": "plan kapalı",
                    "target_disabled": "RadioTEDU hedefi kapalı",
                    "outside_schedule_window": "planın tarih/saat aralığı dışında",
                    "ad_track_inactive_or_missing": "reklam dosyası etkin değil veya eksik",
                }
                text = f"Reklam planı pasif · {reasons.get(str(ad.get('reason')), str(ad.get('reason') or 'uygun hedef yok'))}"
            elif ad.get("state") == "unmaterialized":
                text = f"Reklam zamanı geldi · plan kuyruğa yazılmamış ({ad.get('name') or 'adsız plan'})"
            elif ad.get("state") == "countdown":
                text = f"{int(ad.get('remaining') or 0)} şarkı kaldı · {ad.get('name') or 'reklam planı'}"
            elif ad.get("state") == "unavailable":
                text = "Reklam durumu sağlık API'sinden alınamadı"
            elif ad.get("state") == "unsupported":
                text = "Bu yayın API sürümü reklam kuyruğu/plan bilgisini sunmuyor"
            elif ad.get("state") == "no_plan":
                text = "RadioTEDU için reklam planı yapılandırılmamış"
            elif ad.get("state") == "playing_unverified":
                text = "Reklam kuyruğu çalıyor görünüyor · dinleyici sesi doğrulanmadı"
            else:
                text = "Reklam planı durumu belirlenemedi"
            self.ad_text.configure(text=text)
        else:
            self.ad_text.configure(text="RadioTEDU için reklam planı yok")
        now = datetime.fromtimestamp(data["at"]).strftime("%H:%M:%S")
        suffix = f" · {data['error']}" if data["error"] else ""
        self.updated.configure(text=f"Son güncelleme {now}{suffix}")


if __name__ == "__main__" and _single_instance():
    Monitor().mainloop()
