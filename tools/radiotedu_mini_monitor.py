"""Lightweight, read-only Windows monitor for the RadioTEDU playout sources.

Run with pythonw.exe.  The window reports source health from station worker
heartbeats; it does not equate a connected source with listener delivery.
"""

from __future__ import annotations

import ctypes
import http.client
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont
from datetime import date, datetime, time as day_time, timezone, timedelta
from zoneinfo import ZoneInfo


DATA_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir"
HEARTBEAT_ROOT = DATA_ROOT / "State" / "StationWorkers"
DATABASE = DATA_ROOT / "cleanroom.db"
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
try:
    _LISTENER_RESULTS.update({
        int(key): value for key, value in json.loads(
            (DATA_ROOT / "Logs" / "mini_monitor_listener.json").read_text(encoding="utf-8")
        ).items()
    })
except (OSError, ValueError, TypeError, AttributeError):
    pass


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
                try:
                    (DATA_ROOT / "Logs" / "mini_monitor_listener.json").write_text(
                        json.dumps(_LISTENER_RESULTS, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError:
                    pass
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


def _read_heartbeats() -> dict[int, dict]:
    result: dict[int, dict] = {}
    for station_id, _, _ in STATIONS:
        file = HEARTBEAT_ROOT / f"station-{station_id}.heartbeat.json"
        try:
            result[station_id] = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result[station_id] = {}
    return result


def _plan_window_active(plan, now: datetime | None = None) -> bool:
    """Mirror the planner's date, weekday and local-time eligibility checks."""
    zone = ZoneInfo(str(plan["timezone"] or "Europe/Istanbul"))
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
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


def _ad_plan_snapshot(conn, station_id: int) -> dict:
    """Describe only materialized ad queue rows as queued or playing."""
    playing = conn.execute(
        "SELECT a.id, a.status, a.due_at, COALESCE(t.title,'') AS title, "
        "COALESCE(t.artist,'') AS artist, COALESCE(t.file_path,'') AS file_path "
        "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
        "WHERE a.station_id=? AND a.status='playing' "
        "ORDER BY a.started_at, a.id LIMIT 1",
        (int(station_id),),
    ).fetchone()
    if playing:
        return {"state": "playing", **dict(playing)}

    pending = conn.execute(
        "SELECT a.id, a.status, a.due_at, COALESCE(t.title,'') AS title, "
        "COALESCE(t.artist,'') AS artist, COALESCE(t.file_path,'') AS file_path "
        "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
        "WHERE a.station_id=? AND a.status='pending' "
        "ORDER BY datetime(a.due_at), a.priority DESC, a.id LIMIT 1",
        (int(station_id),),
    ).fetchone()

    plans = conn.execute(
        "SELECT p.id, p.name, p.enabled, p.source_station_id, p.starts_on, p.ends_on, "
        "p.weekdays_json, p.local_start, p.local_end, p.timezone, p.repeat_every_songs, "
        "p.priority, p.created_at, t.track_id AS target_track_id, t.enabled AS target_enabled, "
        "COALESCE(tr.is_active,0) AS track_active, COALESCE(tr.file_path,'') AS track_path "
        "FROM broadcast_plans p "
        "LEFT JOIN broadcast_plan_targets t ON t.plan_id=p.id AND t.station_id=? "
        "LEFT JOIN tracks tr ON tr.id=t.track_id "
        "WHERE p.plan_type='ad' AND p.cadence_mode='songs' "
        "AND EXISTS (SELECT 1 FROM broadcast_plan_targets target "
        "            WHERE target.plan_id=p.id AND target.station_id=?) "
        "ORDER BY p.priority DESC, p.id DESC",
        (int(station_id), int(station_id)),
    ).fetchall()

    active = []
    inactive_reason = ""
    for plan in plans:
        if not bool(plan["enabled"]):
            inactive_reason = "plan_disabled"
            continue
        if not bool(plan["target_enabled"]):
            inactive_reason = "target_disabled"
            continue
        try:
            if not _plan_window_active(plan):
                inactive_reason = "outside_schedule_window"
                continue
        except Exception as exc:
            # ZoneInfoNotFoundError is a ValueError subclass; malformed plan
            # data is reported as inactive instead of being called queued.
            inactive_reason = f"invalid_plan:{type(exc).__name__}"
            continue
        if not bool(plan["track_active"]) or not str(plan["track_path"] or "").strip():
            inactive_reason = "ad_track_inactive_or_missing"
            continue
        active.append(plan)

    if pending:
        payload = {"state": "pending", **dict(pending)}
        if not str(pending["file_path"] or "").strip():
            payload["state"] = "pending_media_missing"
        return payload
    if not active:
        if plans:
            return {"state": "inactive", "reason": inactive_reason or "no_eligible_target"}
        return {"state": "no_plan"}

    plan = active[0]
    interval = max(1, int(plan["repeat_every_songs"] or 10))
    count_row = conn.execute(
        "SELECT COUNT(*) AS amount FROM queue_items q JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=? AND t.station_id=? AND q.status='done' "
        "AND LOWER(COALESCE(t.track_type,'music'))='music' "
        "AND q.finished_at IS NOT NULL AND datetime(q.finished_at)>=datetime(?)",
        (int(station_id), int(station_id), str(plan["created_at"] or "1970-01-01 00:00:00")),
    ).fetchone()
    music_count = int(count_row["amount"] or 0)
    completed_cycles = music_count // interval
    statuses = conn.execute(
        "SELECT dedupe_key, status FROM ad_break_items WHERE station_id=? "
        "AND dedupe_key LIKE ? ORDER BY id DESC",
        (int(station_id), f"broadcast-plan:{int(plan['id'])}:song:{int(station_id)}:%"),
    ).fetchall()
    status_by_cycle: dict[int, str] = {}
    for row in statuses:
        try:
            cycle = int(str(row["dedupe_key"]).rsplit(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            continue
        status_by_cycle.setdefault(cycle, str(row["status"] or ""))
    for cycle in range(1, completed_cycles + 1):
        if status_by_cycle.get(cycle) != "done":
            return {
                "state": "unmaterialized",
                "reason": "due_without_pending_row",
                "name": str(plan["name"] or ""),
                "interval": interval,
                "remaining": 0,
                "cycle": cycle,
            }
    remainder = music_count % interval
    remaining = interval - remainder if remainder else interval
    return {
        "state": "countdown", "name": str(plan["name"] or ""),
        "interval": interval, "remaining": remaining,
    }


def _database_snapshot() -> tuple[dict[int, dict], dict | None, str | None]:
    playout: dict[int, dict] = {}
    ad: dict | None = None
    try:
        conn = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True, timeout=0.7)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=700")
        try:
            # Resolve the row currently owned by playout_state first. A queue
            # status alone cannot identify scheduled, host, or ad playback.
            for station_id, _, _ in STATIONS:
                state = conn.execute(
                    "SELECT current_source, current_item_id FROM playout_state WHERE station_id=?",
                    (station_id,),
                ).fetchone()
                source = str(state["current_source"] or "none") if state else "none"
                item_id = int(state["current_item_id"] or 0) if state else 0
                table_by_source = {
                    "manual": "queue_items",
                    "ads": "ad_break_items",
                    "schedule": "schedule_items",
                    "host": "program_queue_items",
                }
                table = table_by_source.get(source)
                current = None
                if table and item_id > 0:
                    current = conn.execute(
                        "SELECT t.title, t.artist, t.duration, "
                        "COALESCE(t.track_type, 'music') AS track_type "
                        f"FROM {table} q LEFT JOIN tracks t ON t.id=q.track_id "
                        "WHERE q.id=? AND q.station_id=? LIMIT 1",
                        (item_id, station_id),
                    ).fetchone()
                if current:
                    playout[station_id] = dict(current)
                elif source not in ("", "none"):
                    playout[station_id] = {
                        "title": source.replace("_", " ").title(),
                        "artist": "", "track_type": source,
                    }

                # A scheduled item may preempt the ordinary automation queue;
                # display both so the operator can see what follows.
                next_item = conn.execute(
                    "SELECT t.title, t.artist, t.track_type "
                    "FROM queue_items q LEFT JOIN tracks t ON t.id=q.track_id "
                    "WHERE q.station_id=? AND q.status='pending' "
                    "AND (q.retry_after IS NULL OR datetime(q.retry_after)<=CURRENT_TIMESTAMP) "
                    "ORDER BY q.position, q.id LIMIT 1",
                    (station_id,),
                ).fetchone()
                inventory = conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN is_active=1 AND COALESCE(file_path,'')<>'' "
                    "AND LOWER(COALESCE(track_type,'music'))='music' "
                    "AND COALESCE(exclude_from_autoplay,0)=0 THEN 1 ELSE 0 END) AS eligible, "
                    "(SELECT COUNT(*) FROM queue_items q JOIN tracks qt ON qt.id=q.track_id "
                    " WHERE q.station_id=? AND q.status='pending' AND qt.station_id=? "
                    " AND COALESCE(qt.file_path,'')<>'') AS pending "
                    f"FROM tracks WHERE station_id=?",
                    (station_id, station_id, station_id),
                ).fetchone()
                playout.setdefault(station_id, {})
                playout[station_id].update({
                    "next_title": str(next_item["title"] or "") if next_item else "",
                    "next_artist": str(next_item["artist"] or "") if next_item else "",
                    "eligible_count": int(inventory["eligible"] or 0),
                    "pending_count": int(inventory["pending"] or 0),
                })

            ad = _ad_plan_snapshot(conn, station_id=4)
        finally:
            conn.close()
        return playout, ad, None
    except (OSError, sqlite3.Error, ValueError) as exc:
        return playout, ad, f"Veritabanı okunamadı: {type(exc).__name__}"


def _parse_utc(value: object) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (TypeError, ValueError):
        return time.time()


def _snapshot() -> dict:
    heartbeats = _read_heartbeats()
    playing, ad, error = _database_snapshot()
    with _LISTENER_LOCK:
        listeners = dict(_LISTENER_RESULTS)
    return {"heartbeats": heartbeats, "playing": playing, "listeners": listeners,
            "ad": ad, "error": error, "at": time.time()}


def _classify(heartbeat: dict, prior: dict | None, listener: dict | None) -> tuple[str, str]:
    if not heartbeat:
        return "DURDU", "bad"
    age = time.time() - float(heartbeat.get("updated_epoch") or 0)
    if age > 20 or not heartbeat.get("running"):
        return "DURDU", "bad"
    runtime = heartbeat.get("runtime_status") or {}
    health = runtime.get("icecast_mount_health") or {}
    if not runtime.get("program_running"):
        if runtime.get("output_feed_active"):
            return "SES KAYNAĞI DURDU", "warn"
        return "ÜRETİCİ DURDU", "bad"
    write_age = health.get("last_network_write_age_seconds")
    if write_age is None or float(write_age) > 15 or not health.get("process_running"):
        return "KAYNAK KESİK", "bad"
    if health.get("mount_healthy") is False or runtime.get("recovery", {}).get("state") not in (None, "idle"):
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
        except Exception as exc:
            data = {"heartbeats": _read_heartbeats(), "playing": {}, "listeners": {},
                    "ad": None, "error": f"Okuma hatası: {type(exc).__name__}",
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
        self._busy = False
        self._render(data)
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
            state_label.configure(text=state, fg=palette[level])
            playout = data["playing"].get(sid) or {}
            if playout.get("title"):
                artist = str(playout.get("artist") or "").strip()
                title = str(playout.get("title") or "").strip()
                song = f"Şimdi: {artist} · {title}" if artist else f"Şimdi: {title}"
            elif state in {"ÜRETİCİ DURDU", "SES KAYNAĞI DURDU"}:
                song = "Şu anki içerik kaynağı çalışmıyor"
            else:
                song = "Şu an çalan içerik bilgisi yok"
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
                    f"Otomatik uygun müzik: {int(playout.get('eligible_count') or 0)}"
                    f" · sıradaki öğeler: {int(playout.get('pending_count') or 0)}"
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
            else:
                text = "Reklam planı durumu belirlenemedi"
            self.ad_text.configure(text=text)
        else:
            self.ad_text.configure(text="RadioTEDU için reklam planı yok")
        now = datetime.fromtimestamp(data["at"]).strftime("%H:%M:%S")
        suffix = f" · {data['error']}" if data["error"] else ""
        self.updated.configure(text=f"Son güncelleme {now}{suffix}")


if __name__ == "__main__" and _single_instance():
    try:
        Monitor().mainloop()
    except Exception:
        (DATA_ROOT / "Logs" / "mini_monitor_error.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
