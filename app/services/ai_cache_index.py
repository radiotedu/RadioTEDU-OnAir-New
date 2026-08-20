from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any


_SYNC_BUILD_LIMIT = 256


class AnnouncementCacheIndex:
    """Non-blocking dedupe-key index for the on-disk AI announcement cache.

    Large RadioTEDU caches contain thousands of metadata files.  A station
    scheduler must never parse that directory in its one-second playout tick,
    so large first builds and refreshes happen on a daemon thread.  Small test
    and fresh-install caches are indexed synchronously.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self._lock = threading.RLock()
        self._by_dedupe: dict[str, list[dict[str, Any]]] = {}
        self._registered_during_build: dict[str, list[dict[str, Any]]] = {}
        self._ready = False
        self._building = False
        self._directory_mtime_ns = -1
        self._refresh_pending = False

    def _current_directory_mtime_ns(self) -> int:
        try:
            return int(self.cache_dir.stat().st_mtime_ns)
        except OSError:
            return -1

    @staticmethod
    def _valid_payload(payload: dict[str, Any]) -> bool:
        dedupe_key = str(payload.get("dedupe_key", "") or "").strip()
        audio_path = str(payload.get("audio_path", "") or "").strip()
        return bool(dedupe_key and audio_path and Path(audio_path).is_file())

    def _scan(self) -> tuple[dict[str, list[dict[str, Any]]], int]:
        indexed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        try:
            paths = self.cache_dir.glob("announcement_*.json")
            for metadata_path in paths:
                try:
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict) or not self._valid_payload(payload):
                        continue
                    payload = dict(payload)
                    payload["_metadata_mtime_ns"] = int(metadata_path.stat().st_mtime_ns)
                    indexed[str(payload["dedupe_key"]).strip()].append(payload)
                except (OSError, UnicodeError, ValueError, TypeError):
                    continue
        except OSError:
            pass
        for values in indexed.values():
            values.sort(
                key=lambda item: int(item.get("_metadata_mtime_ns", 0) or 0),
                reverse=True,
            )
        return dict(indexed), self._current_directory_mtime_ns()

    def _publish_scan(self) -> None:
        rebuild = False
        try:
            indexed, directory_mtime_ns = self._scan()
            with self._lock:
                for key, registered in self._registered_during_build.items():
                    cache_keys = {
                        str(item.get("cache_key", "") or "")
                        for item in registered
                    }
                    indexed[key] = list(registered) + [
                        item
                        for item in indexed.get(key, ())
                        if str(item.get("cache_key", "") or "") not in cache_keys
                    ]
                self._by_dedupe = indexed
                self._registered_during_build = {}
                self._directory_mtime_ns = directory_mtime_ns
                self._ready = True
        finally:
            with self._lock:
                self._building = False
                rebuild = self._refresh_pending
                self._refresh_pending = False
        if rebuild:
            self._start_background_build()

    def _start_background_build(self) -> None:
        with self._lock:
            if self._building:
                return
            self._building = True
        threading.Thread(
            target=self._publish_scan,
            name="ai-announcement-cache-index",
            daemon=True,
        ).start()

    def _ensure_build_started(self) -> None:
        with self._lock:
            ready = self._ready
            building = self._building
            indexed_mtime = self._directory_mtime_ns
        current_mtime = self._current_directory_mtime_ns()
        if ready and current_mtime == indexed_mtime:
            return
        if building:
            # Coalesce changes observed while a scan is in progress into one
            # follow-up refresh. Never start competing scans, and never lose a
            # directory generation that appeared during the current scan.
            if ready and current_mtime != indexed_mtime:
                with self._lock:
                    self._refresh_pending = True
            return
        if not ready:
            # Bound the work performed on the scheduler thread.  Enumerating at
            # most 257 names is cheap; parsing a mature cache is delegated.
            try:
                iterator = self.cache_dir.glob("announcement_*.json")
                sample = []
                for index, path in enumerate(iterator):
                    if index >= _SYNC_BUILD_LIMIT:
                        self._start_background_build()
                        return
                    sample.append(path)
            except OSError:
                sample = []
            # The cache is small enough for a deterministic immediate lookup.
            self._publish_scan()
            return
        self._start_background_build()

    def lookup(
        self,
        dedupe_key: str,
        *,
        expected_tts_provider: str | None = None,
    ) -> dict[str, Any] | None:
        key = str(dedupe_key or "").strip()
        if not key:
            return None
        self._ensure_build_started()
        provider = str(expected_tts_provider or "").strip().lower()
        with self._lock:
            candidates = [dict(item) for item in self._by_dedupe.get(key, ())]
        for payload in candidates:
            payload_provider = str(payload.get("tts_provider", "") or "").strip().lower()
            if provider and payload_provider and payload_provider != provider:
                continue
            if self._valid_payload(payload):
                payload.pop("_metadata_mtime_ns", None)
                return payload
        return None

    def register(self, payload: dict[str, Any]) -> None:
        document = dict(payload or {})
        if not self._valid_payload(document):
            return
        document["_metadata_mtime_ns"] = self._current_directory_mtime_ns()
        key = str(document["dedupe_key"]).strip()
        with self._lock:
            current = list(self._by_dedupe.get(key, ()))
            cache_key = str(document.get("cache_key", "") or "")
            current = [
                item
                for item in current
                if str(item.get("cache_key", "") or "") != cache_key
            ]
            current.insert(0, document)
            self._by_dedupe[key] = current
            registered = list(self._registered_during_build.get(key, ()))
            registered.insert(0, document)
            self._registered_during_build[key] = registered
            # Own-process writes are immediately visible even while the initial
            # background scan is still building.


_indexes: dict[str, AnnouncementCacheIndex] = {}
_indexes_lock = threading.Lock()


def get_announcement_cache_index(cache_dir: Path) -> AnnouncementCacheIndex:
    resolved = str(Path(cache_dir).resolve()).casefold()
    with _indexes_lock:
        index = _indexes.get(resolved)
        if index is None:
            index = AnnouncementCacheIndex(Path(cache_dir).resolve())
            _indexes[resolved] = index
        return index
