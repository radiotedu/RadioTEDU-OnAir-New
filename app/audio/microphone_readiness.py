"""Passive, cached physical microphone readiness evidence for the Health Wall."""

from __future__ import annotations

import os
import re
import threading
import time
import subprocess
from collections.abc import Callable

from app.audio.device_discovery import list_input_devices

_DEFAULT_TTL_SECONDS = 30.0
_LABEL_LIMIT = 120
_UNSAFE_LABEL = re.compile(r"(?:@device|[\\\\/]|\b(?:vid|pid)_[0-9a-f]+\b|\{[0-9a-f-]{8,}\})", re.I)


def _safe_label(value: object) -> str:
    label = " ".join(str(value or "").split()).strip()[:_LABEL_LIMIT]
    if not label or _UNSAFE_LABEL.search(label):
        return ""
    return label


class PhysicalMicrophoneReadiness:
    """Never blocks callers: enumeration happens at most once per TTL in a daemon thread."""

    def __init__(
        self,
        device_lister: Callable[[], list[str]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        enabled: bool | None = None,
    ) -> None:
        self._device_lister = device_lister or list_input_devices
        self._clock = clock
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._enabled = enabled if enabled is not None else os.getenv("CLEANROOM_PHYSICAL_MICROPHONE_DISCOVERY_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        self._lock = threading.Lock()
        self._labels: tuple[str, ...] | None = None
        self._has_result = False
        self._observed_at = 0.0
        self._refreshing = False
        self._refresh_started_at = 0.0
        self._generation = 0
        self._error_code = ""

    def snapshot(self, *, live: bool, receiving: bool) -> dict:
        with self._lock:
            labels = self._labels
            now = self._clock()
            age_seconds = max(0.0, now - self._observed_at) if self._has_result else None
            stale = not self._has_result or (age_seconds is not None and age_seconds >= self._ttl_seconds)
            if self._refreshing and now - self._refresh_started_at > max(6.0, self._ttl_seconds):
                self._refreshing = False
                self._error_code = "enumeration_timeout"
                labels = None
                self._labels = None
            if self._enabled and stale and not self._refreshing:
                self._refreshing = True
                self._refresh_started_at = now
                self._generation += 1
                generation = self._generation
                threading.Thread(target=lambda: self._refresh(generation), daemon=True, name="mic-device-readiness").start()
            refreshing = self._refreshing
            error_code = self._error_code

        configured_label = _safe_label(os.environ.get("CLEANROOM_PHYSICAL_MICROPHONE_LABEL"))
        label = ""
        if not self._enabled:
            presence = "disabled"
            selection = "unknown"
        elif labels is None:
            presence = "unknown"
            selection = "unknown"
        else:
            presence = "present" if labels else "missing"
            matched = next(
                (item for item in labels if configured_label and item.casefold() == configured_label.casefold()),
                "",
            )
            if not configured_label:
                selection = "unknown"
            else:
                selection = "selected" if matched else "not-present"
            label = ""  # Friendly hardware names are intentionally never exposed.

        return {
            "presence": presence,
            "selection": selection,
            "live": "live" if live else "idle",
            "receiving": "receiving" if receiving else "not-receiving",
            "label": label,
            "refreshing": refreshing,
            "discovery": "best_effort_passive" if self._enabled else "disabled",
            "observed_at": int(self._observed_at) if self._has_result else None,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "stale": bool(self._enabled and stale),
            "error_code": error_code,
        }

    def _refresh(self, generation: int | None = None) -> None:
        try:
            labels = tuple(
                item for item in (_safe_label(value) for value in self._device_lister()) if item
            )
            error_code = ""
        except FileNotFoundError:
            labels = None
            error_code = "ffmpeg_unavailable"
        except subprocess.TimeoutExpired:
            labels = None
            error_code = "enumeration_timeout"
        except Exception:
            labels = None
            error_code = "enumeration_failed"
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._labels = labels
            self._has_result = True
            self._observed_at = self._clock()
            self._refreshing = False
            self._error_code = error_code


physical_microphone_readiness = PhysicalMicrophoneReadiness()
