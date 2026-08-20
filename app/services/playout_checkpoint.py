from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from app.db import get_connection, init_db
from app.services.ha_coordinator import ha_coordinator


class PlayoutCheckpointService:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self.last_error = ""

    def _media_checksum(self, raw_path: str) -> str:
        path = Path(str(raw_path or ""))
        if not path.is_file():
            return ""
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        cached = self._hash_cache.get(key)
        if cached:
            return cached
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self._hash_cache = {key: value}
        return value

    def capture(self) -> list[dict]:
        init_db()
        from app.api.runtime import runtime_registry

        conn = get_connection()
        try:
            station_ids = [int(row[0]) for row in conn.execute("SELECT id FROM stations ORDER BY id").fetchall()]
            checkpoints = []
            for station_id in station_ids:
                status = runtime_registry.status(station_id)
                queue_head = conn.execute(
                    "SELECT id, track_id, position, status FROM queue_items WHERE station_id=? AND status IN ('playing','pending') ORDER BY CASE status WHEN 'playing' THEN 0 ELSE 1 END, position LIMIT 1",
                    (station_id,),
                ).fetchone()
                active_path = str(status.get("active_input_uri") or "")
                payload = {
                    "station_id": station_id,
                    "running": bool(status.get("running")),
                    "active_input_uri": active_path,
                    "active_media_checksum": self._media_checksum(active_path),
                    "offset_seconds": float(status.get("offset_seconds") or status.get("current_offset_seconds") or 0),
                    "backend": str(status.get("backend") or "none"),
                    "queue_head": dict(queue_head) if queue_head else None,
                    "checkpointed_at": time.time(),
                }
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                checkpoint = {"node_id": ha_coordinator.node_id, "payload": payload, "payload_json": canonical, "checksum": checksum}
                self.store(checkpoint)
                checkpoints.append(checkpoint)
            return checkpoints
        finally:
            conn.close()

    def store(self, checkpoint: dict) -> None:
        payload_json = str(checkpoint.get("payload_json") or json.dumps(checkpoint.get("payload") or {}, sort_keys=True, separators=(",", ":")))
        expected = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if expected != str(checkpoint.get("checksum") or ""):
            raise ValueError("checkpoint_checksum_mismatch")
        payload = json.loads(payload_json)
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO ha_playout_checkpoints(station_id, node_id, payload_json, checksum, checkpointed_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(station_id) DO UPDATE SET "
                "node_id=excluded.node_id, payload_json=excluded.payload_json, checksum=excluded.checksum, "
                "checkpointed_at=excluded.checkpointed_at, received_at=CURRENT_TIMESTAMP",
                (int(payload["station_id"]), str(checkpoint.get("node_id") or ""), payload_json, expected, float(payload["checkpointed_at"])),
            )
            conn.commit()
        finally:
            conn.close()

    def start(self) -> None:
        if self._thread is not None or os.getenv("PYTEST_CURRENT_TEST"):
            return
        self._stop.clear()

        def run():
            while not self._stop.wait(0.5):
                snapshot = ha_coordinator.snapshot()
                if snapshot["enabled"] and snapshot["role"] != "leader":
                    continue
                try:
                    for checkpoint in self.capture():
                        if snapshot["enabled"]:
                            ha_coordinator.replicate_checkpoint(checkpoint)
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = str(exc)[:500]

        self._thread = threading.Thread(target=run, name="onair-playout-checkpoint", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def latest(self, station_id: int) -> dict | None:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM ha_playout_checkpoints WHERE station_id=?", (int(station_id),)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = json.loads(str(row["payload_json"]))
            return result
        finally:
            conn.close()


playout_checkpoint_service = PlayoutCheckpointService()
