from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.runtime_paths import get_data_dir, resolve_binary
from app.services.audit_chain import audit_chain


class ProgramRecordingError(RuntimeError):
    pass


class _Recorder:
    def __init__(self, path: Path):
        ffmpeg = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
        if not ffmpeg:
            raise ProgramRecordingError("ffmpeg_unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        self._failed = ""
        self._process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0", "-c:a", "flac", "-y", str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            # No reader owns stderr.  A PIPE can fill and freeze FFmpeg while
            # the recorder still looks alive, eventually applying pressure to
            # the broadcast PCM fan-out.  Recording health is tracked through
            # process state and the bounded input queue instead.
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self._thread = threading.Thread(target=self._write_loop, name=f"onair-recorder-{path.stem}", daemon=True)
        self._thread.start()

    def _write_loop(self) -> None:
        try:
            while True:
                chunk = self._queue.get()
                if chunk is None:
                    break
                if self._process.stdin is None:
                    raise BrokenPipeError("recorder_stdin_closed")
                self._process.stdin.write(chunk)
        except (OSError, BrokenPipeError) as exc:
            self._failed = str(exc)
        finally:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
            except OSError:
                pass

    def push(self, chunk: bytes) -> bool:
        if self._failed or self._process.poll() is not None:
            return False
        try:
            self._queue.put_nowait(bytes(chunk))
            return True
        except queue.Full:
            self._failed = "recorder_backpressure"
            return False

    def stop(self) -> dict:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=5)
        if self._process.poll() is None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
        return {"path": str(self.path), "failed": self._failed, "returncode": self._process.poll()}


@dataclass
class _ActiveRecording:
    recording_id: int
    primary: _Recorder
    mirrors: list[_Recorder]


class ProgramRecordingService:
    def __init__(self):
        self._lock = threading.RLock()
        self._recorders: dict[int, _ActiveRecording] = {}
        self._consent_timers: dict[int, threading.Timer] = {}
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    @staticmethod
    def _replicate_snapshot(recording_id: int) -> None:
        try:
            from app.services.ha_coordinator import ha_coordinator
            from app.services.replication_journal import replication_journal

            if not ha_coordinator.snapshot()["enabled"]:
                return
            conn = get_connection()
            try:
                row = conn.execute("SELECT * FROM guest_recordings WHERE id=?", (int(recording_id),)).fetchone()
                if row is None:
                    return
                payload = {
                    "recording": dict(row),
                    "consents": [dict(item) for item in conn.execute("SELECT * FROM guest_recording_consents WHERE recording_id=?", (int(recording_id),)).fetchall()],
                }
            finally:
                conn.close()
            journal = replication_journal.append("guest_recording", int(recording_id), "snapshot", payload)
            ha_coordinator.replicate_ordered(through_sequence=int(journal["sequence"]))
        except Exception as exc:
            audit_chain.append(category="recording", action="replication.degraded", payload={"recording_id": int(recording_id), "error": str(exc)})

    def request(self, studio_id: int, *, actor_id: int) -> dict:
        init_db()
        conn = get_connection()
        try:
            studio = conn.execute("SELECT * FROM studios WHERE id=?", (int(studio_id),)).fetchone()
            if studio is None:
                raise ProgramRecordingError("studio_not_found")
            existing = conn.execute("SELECT id FROM guest_recordings WHERE studio_id=? AND status IN ('pending_consent','recording')", (int(studio_id),)).fetchone()
            if existing:
                raise ProgramRecordingError("recording_already_active")
            station_id = int(studio["station_id"])
            retention_raw = SettingsRepository(conn).get_station(station_id).get("guest_recording_retention_days", "30")
            retention = max(1, min(365, int(retention_raw or 30)))
            expires = datetime.now(timezone.utc) + timedelta(days=retention)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO guest_recordings(studio_id, station_id, started_by, expires_at) VALUES (?, ?, ?, ?)",
                (int(studio_id), station_id, int(actor_id), expires.isoformat()),
            )
            recording_id = int(cur.lastrowid)
            sessions = conn.execute("SELECT id FROM guest_sessions WHERE studio_id=? AND status='admitted' AND is_connected=1", (int(studio_id),)).fetchall()
            for session in sessions:
                conn.execute("INSERT INTO guest_recording_consents(recording_id, session_id) VALUES (?, ?)", (recording_id, int(session["id"])))
            conn.commit()
        finally:
            conn.close()
        audit_chain.append(category="recording", action="consent.requested", station_id=station_id, actor_id=actor_id, payload={"recording_id": recording_id, "studio_id": int(studio_id), "guest_count": len(sessions)})
        self._replicate_snapshot(recording_id)
        if not sessions:
            self._start(recording_id)
        else:
            timer = threading.Timer(60.0, self._expire_consent, args=(recording_id,))
            timer.daemon = True
            with self._lock:
                self._consent_timers[recording_id] = timer
            timer.start()
        return self.status(recording_id)

    def _expire_consent(self, recording_id: int) -> None:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE guest_recordings SET status='cancelled', stopped_at=CURRENT_TIMESTAMP, interruption_reason='consent_timeout' "
                "WHERE id=? AND status='pending_consent'",
                (int(recording_id),),
            )
            conn.commit()
        finally:
            conn.close()
        with self._lock:
            self._consent_timers.pop(int(recording_id), None)
        self._replicate_snapshot(recording_id)

    def consent(self, recording_id: int, session_token: str, accepted: bool) -> dict:
        from app.services.guest_room_service import guest_room_service

        session = guest_room_service.authenticate_session(session_token)
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_recording_consents WHERE recording_id=? AND session_id=?", (int(recording_id), int(session["id"]))).fetchone()
            if row is None:
                raise ProgramRecordingError("recording_consent_not_requested")
            decision = "accepted" if accepted else "declined"
            conn.execute("UPDATE guest_recording_consents SET decision=?, decided_at=CURRENT_TIMESTAMP WHERE recording_id=? AND session_id=?", (decision, int(recording_id), int(session["id"])))
            if not accepted:
                conn.execute("UPDATE guest_recordings SET status='cancelled', stopped_at=CURRENT_TIMESTAMP, interruption_reason='consent_declined' WHERE id=?", (int(recording_id),))
            conn.commit()
            pending = int(conn.execute("SELECT COUNT(*) FROM guest_recording_consents WHERE recording_id=? AND decision='pending'", (int(recording_id),)).fetchone()[0])
            declined = int(conn.execute("SELECT COUNT(*) FROM guest_recording_consents WHERE recording_id=? AND decision='declined'", (int(recording_id),)).fetchone()[0])
        finally:
            conn.close()
        if accepted and pending == 0 and declined == 0:
            self._start(recording_id)
        elif not accepted:
            with self._lock:
                timer = self._consent_timers.pop(int(recording_id), None)
            if timer:
                timer.cancel()
        self._replicate_snapshot(recording_id)
        return self.status(recording_id)

    def _start(self, recording_id: int, *, resume: bool = False) -> None:
        with self._lock:
            timer = self._consent_timers.pop(int(recording_id), None)
        if timer:
            timer.cancel()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_recordings WHERE id=?", (int(recording_id),)).fetchone()
            expected_status = "recording" if resume else "pending_consent"
            if row is None or str(row["status"]) != expected_status:
                return
            station_id = int(row["station_id"])
            with self._lock:
                if station_id in self._recorders:
                    return
            now = datetime.now(timezone.utc)
            manifest = json.loads(str(row["manifest_json"] or "{}")) if resume else {}
            segments = list(manifest.get("segments") or [])
            if resume and segments:
                segments[-1].setdefault("stopped_at", now.isoformat())
                segments[-1].setdefault("interruption_reason", "leadership_lost")
            sequence = len(segments) + 1
            path = get_data_dir() / "recordings" / str(station_id) / now.strftime("%Y") / now.strftime("%m") / f"onair-{recording_id}-segment-{sequence:03d}.flac"
            recorder = _Recorder(path)
            mirrors: list[_Recorder] = []
            mirror_paths: list[str] = []
            mirror_error = ""
            mirror_root_raw = str(os.getenv("CLEANROOM_RECORDING_MIRROR_ROOT", "") or "").strip()
            if mirror_root_raw:
                mirror_path = (
                    Path(mirror_root_raw).expanduser()
                    / str(station_id)
                    / now.strftime("%Y")
                    / now.strftime("%m")
                    / path.name
                )
                try:
                    if mirror_path.resolve() != path.resolve():
                        mirrors.append(_Recorder(mirror_path))
                        mirror_paths.append(str(mirror_path))
                except Exception as exc:
                    # An unavailable mirror must not prevent a consented local
                    # recording or stall the broadcast audio path.
                    mirror_error = str(exc)
            with self._lock:
                self._recorders[station_id] = _ActiveRecording(int(recording_id), recorder, mirrors)
            segment = {
                "sequence": sequence,
                "path": str(path),
                "mirror_paths": mirror_paths,
                "started_at": now.isoformat(),
            }
            manifest.update({
                "format": "flac",
                "sample_rate": 48000,
                "channels": 2,
                "mirror_status": "ready" if mirror_paths else ("degraded" if mirror_root_raw else "not_configured"),
                "mirror_error": mirror_error,
                "segments": segments + [segment],
            })
            conn.execute(
                "UPDATE guest_recordings SET status='recording', file_path=?, manifest_json=?, "
                "started_at=COALESCE(started_at, CURRENT_TIMESTAMP), stopped_at=NULL, interruption_reason='' WHERE id=?",
                (str(path), json.dumps(manifest, separators=(",", ":")), int(recording_id)),
            )
            conn.commit()
        finally:
            conn.close()
        audit_chain.append(category="recording", action="resumed" if resume else "started", station_id=station_id, payload={"recording_id": int(recording_id), "segment": sequence})
        self._replicate_snapshot(recording_id)

    def publish_pcm(self, station_id: int, chunk: bytes) -> None:
        with self._lock:
            active = self._recorders.get(int(station_id))
        if active is None:
            return
        if not active.primary.push(chunk):
            # publish_pcm runs on the broadcast thread. Finalizing ffmpeg can
            # take seconds, so always perform it out of band.
            threading.Thread(
                target=self.stop,
                args=(active.recording_id,),
                kwargs={"reason": "recorder_backpressure_or_failure"},
                name=f"onair-recording-stop-{active.recording_id}",
                daemon=True,
            ).start()
            return
        failed_mirrors = [mirror for mirror in active.mirrors if not mirror.push(chunk)]
        if failed_mirrors:
            with self._lock:
                active.mirrors = [mirror for mirror in active.mirrors if mirror not in failed_mirrors]
            threading.Thread(
                target=self._handle_failed_mirrors,
                args=(int(station_id), active.recording_id, failed_mirrors),
                name=f"onair-recording-mirror-stop-{active.recording_id}",
                daemon=True,
            ).start()

    @staticmethod
    def _handle_failed_mirrors(station_id: int, recording_id: int, mirrors: list[_Recorder]) -> None:
        results = [mirror.stop() for mirror in mirrors]
        conn = get_connection()
        try:
            row = conn.execute("SELECT manifest_json FROM guest_recordings WHERE id=?", (int(recording_id),)).fetchone()
            manifest = json.loads(str(row["manifest_json"] or "{}")) if row else {}
            manifest["mirror_status"] = "degraded"
            manifest["mirror_error"] = "mirror_backpressure_or_failure"
            conn.execute(
                "UPDATE guest_recordings SET manifest_json=? WHERE id=?",
                (json.dumps(manifest, separators=(",", ":")), int(recording_id)),
            )
            conn.commit()
        finally:
            conn.close()
        audit_chain.append(
            category="recording",
            action="mirror.degraded",
            station_id=int(station_id),
            payload={"recording_id": int(recording_id), "mirrors": results},
        )

    def stop(self, recording_id: int, *, reason: str = "") -> dict:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_recordings WHERE id=?", (int(recording_id),)).fetchone()
            if row is None:
                raise ProgramRecordingError("recording_not_found")
            station_id = int(row["station_id"])
        finally:
            conn.close()
        with self._lock:
            active = self._recorders.pop(station_id, None)
        result = active.primary.stop() if active else {}
        mirror_results = [mirror.stop() for mirror in active.mirrors] if active else []
        failure = str(reason or result.get("failed") or "")
        continue_after_failover = failure == "leadership_lost"
        status = "recording" if continue_after_failover else ("interrupted" if failure else "completed")
        conn = get_connection()
        try:
            row = conn.execute("SELECT manifest_json FROM guest_recordings WHERE id=?", (int(recording_id),)).fetchone()
            manifest = json.loads(str(row["manifest_json"] or "{}")) if row else {}
            segments = list(manifest.get("segments") or [])
            if segments:
                segments[-1]["stopped_at"] = datetime.now(timezone.utc).isoformat()
                if failure:
                    segments[-1]["interruption_reason"] = failure
                manifest["segments"] = segments
            conn.execute(
                "UPDATE guest_recordings SET status=?, stopped_at=?, interruption_reason=?, manifest_json=? WHERE id=?",
                (status, None if continue_after_failover else datetime.now(timezone.utc).isoformat(), failure, json.dumps(manifest, separators=(",", ":")), int(recording_id)),
            )
            conn.commit()
        finally:
            conn.close()
        audit_chain.append(category="recording", action=status, station_id=station_id, payload={"recording_id": int(recording_id), "reason": failure, "mirrors": mirror_results})
        self._replicate_snapshot(recording_id)
        return self.status(recording_id)

    def resume_replicated_recordings(self) -> int:
        """Open a new segment for recordings left active by a fenced leader."""
        init_db()
        conn = get_connection()
        try:
            recording_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM guest_recordings WHERE status='recording'").fetchall()]
        finally:
            conn.close()
        resumed = 0
        for recording_id in recording_ids:
            try:
                self._start(recording_id, resume=True)
                resumed += 1
            except Exception as exc:
                audit_chain.append(category="recording", action="resume.failed", payload={"recording_id": recording_id, "error": str(exc)})
        return resumed

    def status(self, recording_id: int) -> dict:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_recordings WHERE id=?", (int(recording_id),)).fetchone()
            if row is None:
                raise ProgramRecordingError("recording_not_found")
            payload = dict(row)
            payload["manifest"] = json.loads(str(row["manifest_json"] or "{}"))
            payload.pop("manifest_json", None)
            payload["consents"] = [dict(item) for item in conn.execute("SELECT * FROM guest_recording_consents WHERE recording_id=?", (int(recording_id),)).fetchall()]
            return payload
        finally:
            conn.close()

    def list(self, station_id: int) -> list[dict]:
        init_db()
        conn = get_connection()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM guest_recordings WHERE station_id=? ORDER BY id DESC", (int(station_id),)).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _allowed_recording_roots() -> list[Path]:
        roots = [(get_data_dir() / "recordings").resolve()]
        mirror = str(os.getenv("CLEANROOM_RECORDING_MIRROR_ROOT", "") or "").strip()
        if mirror:
            roots.append(Path(mirror).expanduser().resolve())
        return roots

    def delete(self, recording_id: int, *, actor_id: int | None = None, reason: str = "manual") -> dict:
        recording = self.status(recording_id)
        if recording["status"] in {"recording", "pending_consent"}:
            raise ProgramRecordingError("recording_is_active")
        candidates = {str(recording.get("file_path") or "")}
        for segment in recording.get("manifest", {}).get("segments", []):
            candidates.add(str(segment.get("path") or ""))
            candidates.update(str(path or "") for path in segment.get("mirror_paths", []))
        allowed_roots = self._allowed_recording_roots()
        removed = []
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw).expanduser().resolve()
            if not any(path == root or root in path.parents for root in allowed_roots):
                continue
            if path.is_file():
                path.unlink(missing_ok=True)
                removed.append(str(path))
        conn = get_connection()
        try:
            conn.execute("UPDATE guest_recordings SET status='deleted' WHERE id=?", (int(recording_id),))
            conn.commit()
        finally:
            conn.close()
        audit_chain.append(
            category="recording",
            action="deleted",
            station_id=int(recording["station_id"]),
            actor_id=actor_id,
            payload={"recording_id": int(recording_id), "reason": reason, "files_removed": len(removed)},
        )
        self._replicate_snapshot(recording_id)
        return {"ok": True, "recording_id": int(recording_id), "files_removed": len(removed)}

    def cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            recording_ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM guest_recordings WHERE expires_at<? AND status NOT IN ('recording','pending_consent','deleted')",
                    (now,),
                ).fetchall()
            ]
        finally:
            conn.close()
        removed = 0
        for recording_id in recording_ids:
            try:
                self.delete(recording_id, reason="retention_expired")
                removed += 1
            except Exception:
                pass
        return removed

    def stop_all(self, reason: str = "service_shutdown") -> int:
        with self._lock:
            recording_ids = [active.recording_id for active in self._recorders.values()]
            timers = list(self._consent_timers.values())
            self._consent_timers.clear()
        for timer in timers:
            timer.cancel()
        for recording_id in recording_ids:
            try:
                self.stop(recording_id, reason=reason)
            except Exception:
                pass
        return len(recording_ids)

    def interrupt_station(self, station_id: int, reason: str) -> bool:
        with self._lock:
            active = self._recorders.get(int(station_id))
        if active is None:
            return False
        self.stop(int(active.recording_id), reason=str(reason or "runtime_stopped"))
        return True

    def start_maintenance(self) -> None:
        if self._maintenance_thread is not None:
            return
        self._maintenance_stop.clear()

        def run():
            while not self._maintenance_stop.wait(3600):
                try:
                    self.cleanup_expired()
                except Exception:
                    pass

        self.cleanup_expired()
        self._maintenance_thread = threading.Thread(target=run, name="guest-recording-maintenance", daemon=True)
        self._maintenance_thread.start()

    def stop_maintenance(self) -> None:
        self._maintenance_stop.set()
        if self._maintenance_thread is not None:
            self._maintenance_thread.join(timeout=2)
        self._maintenance_thread = None


program_recording_service = ProgramRecordingService()
