from __future__ import annotations

import ctypes
import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path

from app.audio.bpm_analyzer import analyze_bpm
from app.db import get_connection
from app.media_paths import resolve_runtime_media_path
from app.services.dayparting import station_profile


_log = logging.getLogger("cleanroom.bpm_maintenance")
_SUCCESS_CONFIDENCE = 0.04


def _set_current_thread_below_normal() -> bool:
    """Keep library analysis below every Above-Normal broadcast process."""

    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_bool
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1))
    except (AttributeError, OSError):
        return False


class BpmMaintenanceService:
    """Incrementally populate BPM metadata without competing with live audio."""

    def __init__(self, *, startup_delay_seconds: float = 120.0, interval_seconds: float = 2.0):
        self.startup_delay_seconds = max(0.0, float(startup_delay_seconds))
        self.interval_seconds = max(0.25, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._station_cursor = 0

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="bpm-maintenance",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    @staticmethod
    def _eligible_station_ids(conn: sqlite3.Connection) -> list[int]:
        return [
            int(row["id"])
            for row in conn.execute("SELECT id, name FROM stations ORDER BY id").fetchall()
            if station_profile(str(row["name"] or "")) is not None
        ]

    @staticmethod
    def _next_candidate(conn: sqlite3.Connection, station_id: int):
        return conn.execute(
            "SELECT t.id, t.file_path, COALESCE(t.managed_file_mtime_ns, -1) AS file_mtime_ns "
            "FROM tracks t LEFT JOIN bpm_analysis_state b ON b.track_id=t.id "
            "WHERE t.station_id=? AND t.is_active=1 "
            "AND LOWER(COALESCE(t.track_type, 'music'))='music' "
            "AND COALESCE(t.exclude_from_autoplay, 0)=0 "
            "AND COALESCE(t.file_path, '')<>'' AND COALESCE(t.bpm, 0)<=0 "
            "AND (b.track_id IS NULL OR b.file_mtime_ns<>COALESCE(t.managed_file_mtime_ns, -1) "
            "OR (b.status='error' AND b.attempts<3 "
            "AND b.updated_at<=datetime('now', '-6 hours'))) "
            "ORDER BY COALESCE(t.play_count, 0) DESC, t.id ASC LIMIT 1",
            (int(station_id),),
        ).fetchone()

    @staticmethod
    def _record_result(
        conn: sqlite3.Connection,
        *,
        track_id: int,
        file_mtime_ns: int,
        bpm: float,
        confidence: float,
        status: str,
        error: str = "",
    ) -> None:
        with conn:
            if status == "ok":
                conn.execute("UPDATE tracks SET bpm=? WHERE id=?", (float(bpm), int(track_id)))
            conn.execute(
                "INSERT INTO bpm_analysis_state "
                "(track_id, file_mtime_ns, status, confidence, attempts, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(track_id) DO UPDATE SET "
                "file_mtime_ns=excluded.file_mtime_ns, status=excluded.status, "
                "confidence=excluded.confidence, attempts=bpm_analysis_state.attempts+1, "
                "last_error=excluded.last_error, updated_at=CURRENT_TIMESTAMP",
                (
                    int(track_id),
                    int(file_mtime_ns),
                    str(status),
                    float(confidence),
                    str(error or "")[:300],
                ),
            )

    def run_once(self) -> dict[str, object]:
        conn = get_connection()
        try:
            station_ids = self._eligible_station_ids(conn)
            if not station_ids:
                return {"status": "idle", "reason": "no_daypart_stations"}
            for offset in range(len(station_ids)):
                index = (self._station_cursor + offset) % len(station_ids)
                station_id = station_ids[index]
                candidate = self._next_candidate(conn, station_id)
                if candidate is None:
                    continue
                self._station_cursor = (index + 1) % len(station_ids)
                track_id = int(candidate["id"])
                file_mtime_ns = int(candidate["file_mtime_ns"] or -1)
                file_path = resolve_runtime_media_path(str(candidate["file_path"] or ""))
                try:
                    bpm, confidence = analyze_bpm(file_path, max_seconds=60)
                    status = "ok" if bpm > 0 and confidence >= _SUCCESS_CONFIDENCE else "low_confidence"
                    self._record_result(
                        conn,
                        track_id=track_id,
                        file_mtime_ns=file_mtime_ns,
                        bpm=bpm,
                        confidence=confidence,
                        status=status,
                    )
                    return {
                        "status": status,
                        "station_id": station_id,
                        "track_id": track_id,
                        "bpm": float(bpm),
                        "confidence": float(confidence),
                    }
                except Exception as exc:  # noqa: BLE001 - maintenance must keep progressing
                    self._record_result(
                        conn,
                        track_id=track_id,
                        file_mtime_ns=file_mtime_ns,
                        bpm=0.0,
                        confidence=0.0,
                        status="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    _log.warning("BPM maintenance failed for track_id=%s: %s", track_id, exc)
                    return {
                        "status": "error",
                        "station_id": station_id,
                        "track_id": track_id,
                    }
            return {"status": "idle", "reason": "complete"}
        finally:
            conn.close()

    def _run(self) -> None:
        lowered = _set_current_thread_below_normal()
        _log.info("BPM maintenance scheduled; thread_below_normal=%s", lowered)
        if self._stop.wait(self.startup_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                result = self.run_once()
                if result.get("status") == "idle":
                    if self._stop.wait(3600.0):
                        return
                    continue
            except Exception:
                _log.exception("BPM maintenance iteration failed")
                if self._stop.wait(60.0):
                    return
                continue
            if self._stop.wait(self.interval_seconds):
                return


bpm_maintenance_service = BpmMaintenanceService(
    startup_delay_seconds=float(os.getenv("CLEANROOM_BPM_STARTUP_DELAY_SECONDS", "120") or 120),
    interval_seconds=float(os.getenv("CLEANROOM_BPM_INTERVAL_SECONDS", "2") or 2),
)
