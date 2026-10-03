"""Coalesce routine file heartbeats while retaining immediate state changes."""

from __future__ import annotations

import threading
import time


class WorkerHeartbeatPublisher:
    """Serialize scheduler/liveness writes and share their one-second budget.

    The file is a replaceable health snapshot, not the durable playout journal.
    Queue ownership and advertisement completion continue to use SQLite commits.
    """

    def __init__(self, config, writer, *, clock=time.monotonic, interval=1.0):
        self._config = config
        self._writer = writer
        self._clock = clock
        self._interval = max(0.1, float(interval))
        self._lock = threading.Lock()
        self._last_write = None
        self._last_signature = None

    @staticmethod
    def _signature(payload, status, running):
        status = status or {}
        result = payload.get("last_result")
        result = result if isinstance(result, dict) else {}

        def source_signature(health):
            health = health or {}
            return tuple(health.get(key) for key in (
                "source_response_confirmed", "process_running", "writer_running",
                "writer_failed", "network_failed", "mount_healthy",
            ))

        extras = tuple(
            (item.get("mount"), source_signature(item.get("health")))
            for item in status.get("extra_icecast_mounts") or []
        )
        return (
            bool(running),
            payload.get("last_error"),
            payload.get("failure_count", 0),
            bool(payload.get("scheduler_stalled", False)),
            bool(payload.get("runtime_status_available", True)),
            payload.get("runtime_status_error", ""),
            tuple(result.get(key) for key in (
                "source", "reason", "track_id", "item_id", "queue_item_id",
                "ad_id", "break_id",
            )),
            tuple(status.get(key) for key in (
                "active_input_uri", "backend", "program_running",
                "producer_eof", "producer_draining", "producer_finalizing",
                "transition_active", "live_input_enabled", "live_mic_active",
            )),
            source_signature(status.get("icecast_mount_health")),
            extras,
        )

    def publish(self, payload, *, runtime_status, running=True):
        with self._lock:
            now = self._clock()
            signature = self._signature(payload, runtime_status, running)
            urgent = payload.get("event") in {"ready", "stopped"}
            if (
                not urgent
                and self._last_write is not None
                and now - self._last_write < self._interval
                and signature == self._last_signature
            ):
                return False
            self._writer(
                self._config, payload, runtime_status=runtime_status, running=running
            )
            # Failed writes remain retryable even within the current interval.
            self._last_write = now
            self._last_signature = signature
            return True
