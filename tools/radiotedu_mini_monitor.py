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
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from datetime import datetime


MONITOR_API_HOST = "127.0.0.1"
MONITOR_API_PORT = 18110
MONITOR_API_PATH = "/api/monitor/snapshot"
STATE_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir" / "State" / "StationWorkers"
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
        if not isinstance(playout, dict):
            playout = {"state": "unavailable"}
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
            else {"state": "unsupported"}
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
        tick_age = float(payload.get("scheduler_tick_age_seconds") or 999999)
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
