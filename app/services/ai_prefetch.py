"""
AI Announcement Pre-fetch Service.

Generates AI announcements ahead of time so the station worker never has to wait
for TTS generation during its tick loop. While track N is playing, the announcement
for track N+1 (and beyond) is already generated and cached.

Architecture:
- Runs as a background thread per station
- Scans upcoming queue items for pending announcements
- Generates announcements using edge-tts (fast, cloud-based)
- Stores in the shared AI cache with predictable dedupe keys
- Worker tick just finds ready-made audio files

The worker still decides WHEN to announce (station ID interval, track intro timing),
but the actual audio generation happens asynchronously ahead of time.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.db import get_connection

_log = logging.getLogger("cleanroom.ai_prefetch")

# How far ahead to prefetch (number of upcoming queue items to prepare)
PREFETCH_LOOKAHEAD = 5
# Minimum seconds between prefetch cycles (avoid spamming)
PREFETCH_INTERVAL_SEC = 3.0
# Max announcements to generate per cycle
MAX_GENERATE_PER_CYCLE = PREFETCH_LOOKAHEAD
PREFETCH_FAILURE_COOLDOWN_SEC = 600.0
OMNIVOICE_STARTUP_LOOKAHEAD = 2
OMNIVOICE_STARTUP_MAX_GENERATE = 1
STARTUP_BUFFER_TARGET_DEFAULT = 4
STARTUP_BUFFER_TARGET_MAX = 8


def _configured_tts_provider(settings: dict[str, Any] | None) -> str:
    values = settings or {}
    provider = str(
        values.get("ai_tts_provider")
        or values.get("tts_provider")
        or "local-qwen-tts"
    ).strip().lower()
    if provider not in {"local-qwen-tts", "edge-tts", "omnivoice"}:
        return "local-qwen-tts"
    return provider


def _prefetch_lookahead(settings: dict[str, Any] | None) -> int:
    if _configured_tts_provider(settings) == "omnivoice":
        return 8
    return PREFETCH_LOOKAHEAD


def _bounded_int(raw: Any, default: int, *, minimum: int = 1, maximum: int = 180) -> int:
    try:
        value = int(float(str(raw if raw is not None else default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def startup_buffer_target(settings: dict[str, Any] | None) -> int:
    """Number of ready track-intro announcements required before streaming."""
    payload = settings or {}
    raw = payload.get(
        "ai_ready_track_intro_target",
        payload.get("ai_preload_count", STARTUP_BUFFER_TARGET_DEFAULT),
    )
    return _bounded_int(
        raw,
        STARTUP_BUFFER_TARGET_DEFAULT,
        minimum=1,
        maximum=STARTUP_BUFFER_TARGET_MAX,
    )


def _max_generate_per_cycle(settings: dict[str, Any] | None) -> int:
    if _configured_tts_provider(settings) == "omnivoice":
        return 6
    return max(MAX_GENERATE_PER_CYCLE, startup_buffer_target(settings))


@dataclass
class PrefetchTarget:
    """An upcoming queue item that needs an AI announcement."""
    item_id: int
    title: str
    artist: str
    announcement_type: str  # "track_intro" or "station_id"
    dedupe_key: str
    station_id: int
    station_name: str
    settings: dict[str, Any]


@dataclass
class PrefetchStats:
    """Running statistics for the prefetch service."""
    items_scanned: int = 0
    announcements_generated: int = 0
    cache_hits: int = 0
    errors: int = 0
    last_cycle_time: float = 0.0
    last_cycle_items_generated: int = 0


class AIPrefetchService:
    """
    Background service that pre-generates AI announcements.

    Usage:
        service = AIPrefetchService()
        service.start()  # Starts background thread
        # ... later on shutdown ...
        service.stop()
    """

    def __init__(self):
        self._threads: dict[int, threading.Thread] = {}  # station_id -> thread
        self._running: dict[int, bool] = {}
        self._stats: dict[int, PrefetchStats] = {}
        self._target_failures: dict[tuple[int, str], float] = {}
        self._lock = threading.RLock()

    def start(self, station_id: int) -> None:
        """Start prefetching for a station."""
        with self._lock:
            if station_id in self._threads and self._threads[station_id].is_alive():
                return  # Already running
            self._running[station_id] = True
            self._stats.setdefault(station_id, PrefetchStats())
            t = threading.Thread(
                target=self._run_loop,
                args=(station_id,),
                daemon=True,
                name=f"ai-prefetch-{station_id}",
            )
            self._threads[station_id] = t
            t.start()
            _log.info("AI prefetch started for station %d", station_id)

    def ensure_running(self, station_id: int) -> dict[str, Any]:
        """Restart a station prefetch thread if it stopped unexpectedly."""
        settings = self._get_station_settings(station_id)
        if not self._is_truthy(settings.get("ai_host_enabled", "false")):
            return {
                "station_id": int(station_id),
                "running": False,
                "thread_alive": False,
                "restarted": False,
                "reason": "disabled",
            }

        with self._lock:
            thread = self._threads.get(station_id)
            alive = bool(thread and thread.is_alive() and self._running.get(station_id, False))
        if alive:
            return {
                "station_id": int(station_id),
                "running": True,
                "thread_alive": True,
                "restarted": False,
            }

        self.start(station_id)
        return {
            "station_id": int(station_id),
            "running": True,
            "thread_alive": True,
            "restarted": True,
        }

    def stop(self, station_id: int) -> None:
        """Stop prefetching for a station."""
        with self._lock:
            self._running[station_id] = False

    def stop_all(self) -> dict[str, int]:
        """Stop all prefetch threads."""
        with self._lock:
            for sid in list(self._running.keys()):
                self._running[sid] = False
            count = len(self._threads)
            return {"stopped": count}

    def get_stats(self, station_id: int) -> dict[str, Any]:
        """Get prefetch statistics for a station."""
        with self._lock:
            stats = self._stats.get(station_id, PrefetchStats())
            thread = self._threads.get(station_id)
            return {
                "station_id": station_id,
                "running": self._running.get(station_id, False),
                "thread_alive": thread.is_alive() if thread else False,
                "items_scanned": stats.items_scanned,
                "announcements_generated": stats.announcements_generated,
                "cache_hits": stats.cache_hits,
                "errors": stats.errors,
                "last_cycle_items_generated": stats.last_cycle_items_generated,
            }

    def get_all_stats(self) -> list[dict[str, Any]]:
        """Get stats for all stations."""
        with self._lock:
            return [self.get_stats(sid) for sid in list(self._stats.keys())]

    def prime_station(self, station_id: int, *, max_generate: int = 2) -> dict[str, Any]:
        """Synchronously pre-generate the first upcoming announcements for startup."""
        return self._prime_station_impl(
            station_id,
            max_generate=max_generate,
            allow_slow_tts=False,
            lookahead=None,
        )

    def startup_prime_station(self, station_id: int) -> dict[str, Any]:
        """Provider-aware startup prime that builds the pre-stream intro buffer."""
        settings = self._get_station_settings(station_id)
        provider = _configured_tts_provider(settings)
        target = startup_buffer_target(settings)
        lookahead = max(target, _prefetch_lookahead(settings))
        if provider == "omnivoice":
            return self._prime_station_impl(
                station_id,
                max_generate=OMNIVOICE_STARTUP_MAX_GENERATE,
                allow_slow_tts=True,
                lookahead=OMNIVOICE_STARTUP_LOOKAHEAD,
                track_intros_only=True,
            )
        return self._prime_station_impl(
            station_id,
            max_generate=target,
            allow_slow_tts=True,
            lookahead=lookahead,
            track_intros_only=True,
        )

    def _prime_station_impl(
        self,
        station_id: int,
        *,
        max_generate: int,
        allow_slow_tts: bool,
        lookahead: int | None,
        track_intros_only: bool = False,
    ) -> dict[str, Any]:
        """Internal startup prime helper with optional slow-provider support."""
        settings = self._get_station_settings(station_id)
        if not self._is_truthy(settings.get("ai_host_enabled", "false")):
            return {"station_id": station_id, "generated": 0, "targets": 0, "reason": "disabled"}
        provider = _configured_tts_provider(settings)
        if provider == "omnivoice" and not allow_slow_tts:
            return {"station_id": station_id, "generated": 0, "targets": 0, "reason": "slow_tts_provider"}

        station_name = self._get_station_name(station_id)
        lookahead_count = max(1, int(lookahead or _prefetch_lookahead(settings)))
        upcoming = self._get_upcoming_items(station_id, lookahead_count)
        if not upcoming:
            return {"station_id": station_id, "generated": 0, "targets": 0, "reason": "queue_empty"}

        targets = self._identify_targets(station_id, upcoming, station_name, settings)
        if track_intros_only:
            targets = [
                target
                for target in targets
                if target.announcement_type == "track_intro"
            ]
        generated = self._generate_targets(
            station_id=station_id,
            targets=targets,
            settings=settings,
            max_generate=max_generate,
        )
        return {
            "station_id": station_id,
            "generated": generated,
            "targets": len(targets),
            "lookahead": lookahead_count,
            "tts_provider": provider,
            "allow_slow_tts": bool(allow_slow_tts),
            "track_intros_only": bool(track_intros_only),
            "startup_buffer_target": startup_buffer_target(settings),
        }

    def batch_generate_station(
        self,
        station_id: int,
        *,
        max_generate: int = 8,
        lookahead: int | None = None,
        track_intros_only: bool = True,
    ) -> dict[str, Any]:
        """Synchronously generate a batch of upcoming announcements, including slow TTS providers."""
        settings = self._get_station_settings(station_id)
        if not self._is_truthy(settings.get("ai_host_enabled", "false")):
            return {"station_id": station_id, "generated": 0, "targets": 0, "reason": "disabled"}

        station_name = self._get_station_name(station_id)
        lookahead_count = max(1, int(lookahead or _prefetch_lookahead(settings)))
        upcoming = self._get_upcoming_items(station_id, lookahead_count)
        if not upcoming:
            return {"station_id": station_id, "generated": 0, "targets": 0, "reason": "queue_empty"}

        targets = self._identify_targets(station_id, upcoming, station_name, settings)
        if track_intros_only:
            targets = [target for target in targets if target.announcement_type == "track_intro"]

        generated = self._generate_targets(
            station_id=station_id,
            targets=targets,
            settings=settings,
            max_generate=max_generate,
        )
        return {
            "station_id": station_id,
            "generated": generated,
            "targets": len(targets),
            "lookahead": lookahead_count,
            "tts_provider": _configured_tts_provider(settings),
            "track_intros_only": bool(track_intros_only),
        }

    def readiness_snapshot(self, station_id: int, *, lookahead: int | None = None) -> dict[str, Any]:
        """Summarize how much upcoming AI intro buffer is already ready on disk."""
        settings = self._get_station_settings(station_id)
        provider = _configured_tts_provider(settings)
        lookahead_count = max(1, int(lookahead or _prefetch_lookahead(settings)))
        upcoming = self._get_upcoming_items(station_id, lookahead_count)
        music_items = [
            item for item in upcoming
            if str(item.get("track_type") or "music").strip().lower() == "music"
            and str(item.get("title") or "").strip()
        ]
        ready_count = 0
        covered_seconds = 0.0
        missing_item_ids: list[int] = []
        for item in music_items:
            item_id = int(item["id"])
            ready = self._is_cached(
                f"ai-track-intro:{item_id}",
                expected_tts_provider=provider,
            )
            if ready:
                ready_count += 1
                if not missing_item_ids:
                    covered_seconds += max(0.0, float(item.get("duration") or 0.0))
                continue
            missing_item_ids.append(item_id)

        stats = self.get_stats(station_id)
        return {
            "station_id": int(station_id),
            "tts_provider": provider,
            "lookahead": lookahead_count,
            "upcoming_music_items": len(music_items),
            "ready_track_intros": ready_count,
            "covered_music_seconds": round(covered_seconds, 2),
            "next_missing_track_intro_item_ids": missing_item_ids[:5],
            "prefetch_running": bool(stats.get("running", False)),
            "prefetch_thread_alive": bool(stats.get("thread_alive", False)),
            "ai_host_enabled": self._is_truthy(settings.get("ai_host_enabled", "false")),
        }

    # ── Internal ─────────────────────────────────────────────────────────

    def _run_loop(self, station_id: int) -> None:
        """Main prefetch loop for a station."""
        import time

        _log.info("Prefetch loop starting for station %d", station_id)
        consecutive_errors = 0

        while self._running.get(station_id, False):
            try:
                self._run_cycle(station_id)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                _log.warning(
                    "Prefetch cycle error for station %d (consecutive: %d): %s",
                    station_id, consecutive_errors, exc,
                    exc_info=True,
                )
                if consecutive_errors >= 10:
                    _log.error("Too many prefetch errors for station %d, stopping", station_id)
                    self.stop(station_id)
                    break

            # Sleep between cycles
            for _ in range(int(PREFETCH_INTERVAL_SEC * 10)):
                if not self._running.get(station_id, False):
                    break
                time.sleep(0.1)

        _log.info("Prefetch loop stopped for station %d", station_id)

    def _run_cycle(self, station_id: int) -> None:
        """Single prefetch cycle: scan queue and generate pending announcements."""
        # Get station settings
        settings = self._get_station_settings(station_id)
        if not self._is_truthy(settings.get("ai_host_enabled", "false")):
            return  # AI not enabled, skip

        station_name = self._get_station_name(station_id)

        # Get upcoming queue items
        upcoming = self._get_upcoming_items(
            station_id,
            max(_prefetch_lookahead(settings), startup_buffer_target(settings)),
        )
        if not upcoming:
            return

        with self._lock:
            self._stats[station_id].items_scanned += len(upcoming)

        # Determine what announcements are needed
        targets = self._identify_targets(station_id, upcoming, station_name, settings)
        try:
            required_intros = startup_buffer_target(settings)
            readiness = self.readiness_snapshot(station_id, lookahead=required_intros)
            if int(readiness.get("ready_track_intros", 0) or 0) < required_intros:
                targets = [
                    target
                    for target in targets
                    if target.announcement_type == "track_intro"
                ]
        except Exception:
            targets = [
                target
                for target in targets
                if target.announcement_type == "track_intro"
            ]
        generated = self._generate_targets(
            station_id=station_id,
            targets=targets,
            settings=settings,
            max_generate=_max_generate_per_cycle(settings),
        )

        with self._lock:
            self._stats[station_id].last_cycle_time = time.monotonic()
            self._stats[station_id].last_cycle_items_generated = generated

    def _target_skip_remaining_seconds(self, station_id: int, dedupe_key: str) -> float:
        key = (int(station_id), str(dedupe_key or ""))
        with self._lock:
            skip_until = float(self._target_failures.get(key, 0.0) or 0.0)
        return max(0.0, skip_until - time.monotonic())

    def _record_target_failure(self, station_id: int, dedupe_key: str) -> None:
        key = (int(station_id), str(dedupe_key or ""))
        if not key[1]:
            return
        with self._lock:
            self._target_failures[key] = time.monotonic() + PREFETCH_FAILURE_COOLDOWN_SEC

    def _clear_target_failure(self, station_id: int, dedupe_key: str) -> None:
        key = (int(station_id), str(dedupe_key or ""))
        with self._lock:
            self._target_failures.pop(key, None)

    def _generate_targets(
        self,
        *,
        station_id: int,
        targets: list[PrefetchTarget],
        settings: dict[str, Any],
        max_generate: int,
    ) -> int:
        generated = 0
        try:
            try:
                from app.services.ai_host_fast import get_ai_host_fast

                ai = get_ai_host_fast()
            except Exception:
                from app.services.ai_host import get_ai_host

                ai = get_ai_host()
        except Exception:
            with self._lock:
                self._stats.setdefault(station_id, PrefetchStats()).errors += 1
            return 0

        limit = max(0, int(max_generate))
        for target in targets:
            if limit and generated >= limit:
                break
            if self._target_skip_remaining_seconds(station_id, target.dedupe_key) > 0.0:
                continue

            if self._is_cached(target.dedupe_key, expected_tts_provider=_configured_tts_provider(settings)):
                cached_payload = self._cached_announcement_payload(
                    target.dedupe_key,
                    expected_tts_provider=_configured_tts_provider(settings),
                )
                if cached_payload:
                    self._ensure_target_queued(target, cached_payload)
                self._clear_target_failure(station_id, target.dedupe_key)
                with self._lock:
                    self._stats.setdefault(station_id, PrefetchStats()).cache_hits += 1
                continue

            try:
                announcement = self._generate_announcement(ai, target, settings)
                if announcement:
                    generated += 1
                    self._ensure_target_queued(target, announcement)
                    self._clear_target_failure(station_id, target.dedupe_key)
                    with self._lock:
                        self._stats.setdefault(station_id, PrefetchStats()).announcements_generated += 1
                    announcement_text = (
                        announcement.get("text", "")
                        if isinstance(announcement, dict)
                        else getattr(announcement, "text", "")
                    )
                    _log.info(
                        "Prefetch generated %s for item %d: %s",
                        target.announcement_type,
                        target.item_id,
                        str(announcement_text or "")[:60],
                    )
                else:
                    self._record_target_failure(station_id, target.dedupe_key)
                    with self._lock:
                        self._stats.setdefault(station_id, PrefetchStats()).errors += 1
            except Exception as exc:
                _log.warning(
                    "Prefetch failed for %s item %d: %s",
                    target.announcement_type, target.item_id, exc,
                )
                self._record_target_failure(station_id, target.dedupe_key)
                with self._lock:
                    self._stats.setdefault(station_id, PrefetchStats()).errors += 1

        return generated

    def _identify_targets(
        self,
        station_id: int,
        upcoming_items: list[dict[str, Any]],
        station_name: str,
        settings: dict[str, Any],
    ) -> list[PrefetchTarget]:
        """Identify which upcoming items need announcements."""
        targets: list[PrefetchTarget] = []
        first_music_item: dict[str, Any] | None = None
        now_utc = datetime.now(timezone.utc)

        station_id_interval = self._int_setting(
            settings.get("ai_station_id_interval", "1800"),
            1800,
            minimum=0,
            maximum=86400,
        )
        last_station_id_at = self._parse_setting_timestamp(
            settings.get("_ai_last_station_id_at", "")
        )

        for item in upcoming_items:
            item_id = int(item["id"])
            title = str(item.get("title") or "").strip()
            artist = str(item.get("artist") or "").strip()
            track_type = str(item.get("track_type") or "music").strip().lower() or "music"

            if not title or track_type != "music":
                continue
            if first_music_item is None:
                first_music_item = item

            # Track intro announcement
            intro_key = f"ai-track-intro:{item_id}"
            targets.append(PrefetchTarget(
                item_id=item_id,
                title=title,
                artist=artist,
                announcement_type="track_intro",
                dedupe_key=intro_key,
                station_id=station_id,
                station_name=station_name,
                settings=settings,
            ))

        # Track intros prevent dead air. When a station ID is due, prefetch only
        # the earliest one instead of spending every cycle on duplicate variants.
        if first_music_item is not None and station_id_interval > 0 and (
            last_station_id_at is None
            or now_utc - last_station_id_at >= timedelta(seconds=station_id_interval)
        ):
            first_item_id = int(first_music_item["id"])
            sid_key = f"ai-station-id:{first_item_id}"
            targets.append(PrefetchTarget(
                item_id=first_item_id,
                title=str(first_music_item.get("title") or "").strip(),
                artist=str(first_music_item.get("artist") or "").strip(),
                announcement_type="station_id",
                dedupe_key=sid_key,
                station_id=station_id,
                station_name=station_name,
                settings=settings,
            ))

        return targets

    def _generate_announcement(
        self,
        ai,
        target: PrefetchTarget,
        settings: dict[str, Any],
    ):
        """Generate an AI announcement for a prefetch target."""
        if target.announcement_type == "track_intro":
            return ai.generate_track_intro_announcement(
                station_id=target.station_id,
                station_name=target.station_name,
                title=target.title,
                artist=target.artist,
                settings=settings,
                dedupe_key=target.dedupe_key,
            )
        elif target.announcement_type == "station_id":
            return ai.generate_station_id_announcement(
                station_id=target.station_id,
                station_name=target.station_name,
                settings=settings,
                dedupe_key=target.dedupe_key,
            )
        return None

    def _ensure_target_queued(self, target: PrefetchTarget, announcement: Any) -> bool:
        """Insert a ready track intro before its music item while still warming."""
        if target.announcement_type != "track_intro":
            return False
        current_settings = self._get_station_settings(int(target.station_id))
        if not self._is_truthy(current_settings.get("ai_host_enabled", "false")):
            return False

        def _field(name: str, default: Any = "") -> Any:
            if isinstance(announcement, dict):
                return announcement.get(name, default)
            return getattr(announcement, name, default)

        audio_path = str(_field("audio_path", "") or "").strip()
        if not audio_path or not Path(audio_path).exists():
            return False
        provider = str(_field("tts_provider", "") or "").strip().lower()
        if provider != _configured_tts_provider(target.settings):
            return False

        conn = None
        try:
            from app.repositories.queue_repo import QueueRepository
            from app.repositories.track_repo import TrackRepository

            conn = get_connection()
            track_id = TrackRepository(conn).upsert_generated_announcement(
                station_id=int(target.station_id),
                title=str(_field("title", f"AI Intro - {target.title}") or f"AI Intro - {target.title}"),
                file_path=audio_path,
                duration=float(_field("duration_seconds", 0.0) or 0.0),
                artist=str(_field("artist", "AI Host") or "AI Host"),
            )
            _item_id, created = QueueRepository(conn).insert_before_item_or_get_existing(
                station_id=int(target.station_id),
                before_item_id=int(target.item_id),
                track_id=int(track_id),
                dedupe_key=str(target.dedupe_key),
                dedupe_statuses=("pending", "playing", "done"),
            )
            return bool(created)
        except Exception:
            _log.warning(
                "Failed to queue prefetched AI intro for item %d",
                target.item_id,
                exc_info=True,
            )
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _is_cached(self, dedupe_key: str, *, expected_tts_provider: str | None = None) -> bool:
        """Check if a matching cached announcement already exists for this dedupe key."""
        from app.services.ai_host import CACHE_DIR
        from app.services.ai_cache_index import get_announcement_cache_index

        return get_announcement_cache_index(CACHE_DIR).lookup(
            dedupe_key,
            expected_tts_provider=expected_tts_provider,
        ) is not None

    # ── Database helpers ────────────────────────────────────────────────

    def _cached_announcement_payload(
        self,
        dedupe_key: str,
        *,
        expected_tts_provider: str | None = None,
    ) -> dict[str, Any] | None:
        from app.services.ai_host import CACHE_DIR
        from app.services.ai_cache_index import get_announcement_cache_index

        return get_announcement_cache_index(CACHE_DIR).lookup(
            dedupe_key,
            expected_tts_provider=expected_tts_provider,
        )

    @staticmethod
    def _get_station_settings(station_id: int) -> dict[str, Any]:
        """Get station settings from database."""
        from app.repositories.settings_repo import SettingsRepository
        conn = None
        try:
            conn = get_connection()
            repo = SettingsRepository(conn)
            return dict(repo.get_station(station_id))
        except Exception:
            return {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _get_station_name(station_id: int) -> str:
        """Get station name from database."""
        from app.repositories.station_repo import StationRepository
        conn = None
        try:
            conn = get_connection()
            repo = StationRepository(conn)
            for row in repo.list_all():
                if int(row["id"]) == int(station_id):
                    return str(row["name"] or "Main Radio")
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return "Main Radio"

    @staticmethod
    def _get_upcoming_items(station_id: int, count: int) -> list[dict[str, Any]]:
        """Get pending queue rows covering the next N music tracks."""
        from app.repositories.queue_repo import QueueRepository
        from app.services.broadcast_campaign import BroadcastCampaignService
        conn = None
        try:
            conn = get_connection()
            repo = QueueRepository(conn)
            campaign_service = BroadcastCampaignService(conn)
            rows = list(repo.list_active_ordered(station_id))
            pending: list[dict[str, Any]] = []
            target_music = max(0, int(count))
            music_seen = 0
            for row in rows:
                if str(row["status"] or "").strip().lower() != "pending":
                    continue
                item = dict(row)
                if (
                    str(item.get("track_type") or "music").strip().lower() == "music"
                    and not campaign_service.ai_track_allowed(
                        station_id=int(station_id),
                        track_id=int(item.get("track_id") or 0),
                    )
                ):
                    continue
                pending.append(item)
                if str(item.get("track_type") or "music").strip().lower() == "music":
                    music_seen += 1
                    if music_seen >= target_music:
                        break
            return pending
        except Exception:
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _is_truthy(value: str) -> bool:
        """Check if a string value is truthy."""
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _int_setting(value: Any, default: int, *, minimum: int = 1, maximum: int = 180) -> int:
        """Parse an integer setting with bounds."""
        try:
            v = int(float(str(value or default)))
        except (TypeError, ValueError):
            v = default
        return max(minimum, min(maximum, v))

    @staticmethod
    def _parse_setting_timestamp(value: str):
        """Parse a timestamp string to datetime."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None


# ── Singleton ──────────────────────────────────────────────────────────────

_instance: Optional[AIPrefetchService] = None
_instance_lock = threading.Lock()


def get_ai_prefetch() -> AIPrefetchService:
    """Get or create the AI prefetch service singleton."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = AIPrefetchService()
        return _instance


def reset_ai_prefetch() -> None:
    """Reset the prefetch singleton (for testing)."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.stop_all()
        _instance = None
