import logging
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from app.audio.ffmpeg_pipeline import prefetch_fast_cached_uri
from app.audio.virtual_sources import is_silence_input_uri
from app.db import get_connection
from app.engine.lease import LeaseService
from app.engine.ad_policy import ads_enabled_from_settings, station_ads_enabled
from app.media_paths import resolve_runtime_media_path
from app.engine.playout_state import PlayoutStateService
from app.engine.priority import choose_source
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.program_queue_repo import ProgramQueueRepository
from app.repositories.queue_repo import QueueRepository
from app.repositories.schedule_repo import ScheduleRepository
from app.repositories.settings_repo import SettingsRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository
from app.repositories.track_repo import TrackRepository
from app.services.music_usage import MusicUsageService
from app.services.dayparting import active_daypart
from app.services.track_naming import clean_album_metadata

_log = logging.getLogger("cleanroom.worker")

# Module-level debounce state for show notifications (keyed by 3-tuple).
# Prevents broadcast storms when worker fires every second.
_show_notification_sent: dict[tuple, float] = {}
_SHOW_QUEUE_LOW_DEBOUNCE_SEC = 60.0
_AD_BREAK_UPCOMING_DEBOUNCE_SEC = 60.0
# Compatibility state for bounded restart suppression. The clean runtime's
# output-recovery registry owns retry timing, while older guard paths and their
# regression suite still clear this station/item map between runs.
_RESTART_SUPPRESSION: dict[
    tuple[int, str, int],
    dict[str, float | int | str],
] = {}
_MAX_RESTART_ATTEMPTS_PER_ITEM = 3
_RESTART_COOLDOWN_SEC = 180.0
_TRANSIENT_OUTPUT_FAILURE_MARKERS = (
    "error number -10054",
    "error number -10053",
    "remote end closed",
    "error submitting a packet to the muxer",
    "error muxing a packet",
    "transition input unavailable",
    "no such file",
    "file not found",
    "cannot open",
    "invalid data found",
)


class StationWorker:
    def __init__(
        self,
        station_id: int,
        worker_id: str = "worker-1",
        runtime_registry=None,
        fallback_uri: str = "",
        lease_seconds: int = 30,
    ):
        self.station_id = station_id
        self.worker_id = worker_id
        self.conn = get_connection()
        self.queue_repo = QueueRepository(self.conn)
        self.program_queue_repo = ProgramQueueRepository(self.conn)
        self.ad_repo = AdBreakRepository(self.conn)
        self.schedule_repo = ScheduleRepository(self.conn)
        self.lease_service = LeaseService(
            self.conn, lease_seconds=max(3, int(lease_seconds))
        )
        self.playout_state = PlayoutStateService(self.conn)
        self.runtime_registry = runtime_registry
        self.fallback_uri = fallback_uri

    def decide_next_source(
        self, manual_count: int, ad_due: bool, schedule_ready: bool, fallback_ready: bool
    ) -> str:
        return choose_source(manual_count, ad_due, schedule_ready, fallback_ready)

    @staticmethod
    def _is_transient_output_failure(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        if any(marker in text for marker in ("transition input unavailable", "no such file", "file not found", "cannot open")):
            return True
        if "icecast source failed during startup" not in text:
            return False
        return any(marker in text for marker in _TRANSIENT_OUTPUT_FAILURE_MARKERS)

    def _restore_managed_item_pending(self, source: str, item_id: int) -> None:
        table = {
            "manual": "queue_items",
            "ads": "ad_break_items",
            "schedule": "schedule_items",
        }.get(str(source or "").strip().lower())
        if not table:
            return
        cur = self.conn.cursor()
        if table == "queue_items":
            cur.execute(
                "UPDATE queue_items SET status='pending', started_at=NULL, finished_at=NULL WHERE id=?",
                (int(item_id),),
            )
        else:
            cur.execute(f"UPDATE {table} SET status='pending' WHERE id=?", (int(item_id),))
        self.conn.commit()

    @staticmethod
    def _fallback_title_from_uri(track_uri: str) -> str:
        token = str(track_uri or "").strip()
        if not token:
            return ""
        token = token.split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
        if not token:
            return ""
        name = unquote(token.replace("\\", "/").rsplit("/", 1)[-1])
        stem = Path(name).stem or name
        cleaned = " ".join(
            part for part in stem.replace("_", " ").replace("-", " ").split() if part
        )
        return cleaned or stem

    def _default_crossfade_seconds(self) -> float:
        raw_value = SettingsRepository(self.conn).get_system().get(
            "default_crossfade_seconds", 0.0
        )
        try:
            return max(0.0, float(raw_value or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _start_runtime_station(self, *args, stream_album: str = "", **kwargs):
        """Start playback while remaining compatible with older test runtimes.

        ``stream_album`` is optional at the runtime boundary so existing
        lightweight fakes (which predate album metadata) continue to work.
        Production runtimes receive it whenever a track actually has an album.
        """
        if str(stream_album or "").strip():
            kwargs["stream_album"] = str(stream_album).strip()
        return self.runtime_registry.start_station(*args, **kwargs)

    def _track_runtime_fields(self, track_id: int) -> tuple[str, str, str, str, str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT file_path, COALESCE(title, '') AS title, COALESCE(artist, '') AS artist, "
            "COALESCE(album, '') AS album, "
            "COALESCE(track_type, 'music') AS track_type "
            "FROM tracks WHERE id=?",
            (track_id,),
        )
        row = cur.fetchone()
        if not row:
            return "", "", "", "", "music"
        file_path = resolve_runtime_media_path(str(row["file_path"] or ""))
        title = str(row["title"] or "").strip()
        artist = str(row["artist"] or "").strip()
        try:
            album = clean_album_metadata(row["album"])
        except (IndexError, KeyError):
            album = ""
        track_type = str(row["track_type"] or "music").strip().lower() or "music"
        if not title and not artist:
            title = self._fallback_title_from_uri(file_path)
        return (
            file_path,
            title,
            artist,
            album,
            track_type,
        )

    def _broadcast_worker_state(self, *, include_queue: bool = False, include_track: bool = False) -> None:
        try:
            from app.api.legacy import legacy_liquidsoap_status, list_legacy_queue
            from app.ws.broadcaster import broadcaster

            status_payload = legacy_liquidsoap_status(station_id=self.station_id)
            broadcaster.on_runtime_updated(self.station_id, status_payload)
            broadcaster.on_engine_event(self.station_id, status_payload)
            if include_track:
                broadcaster.on_track_changed(self.station_id, status_payload)
            if include_queue:
                broadcaster.on_queue_changed(self.station_id, list_legacy_queue(self.station_id))
        except Exception:
            # Runtime fan-out must never break worker progress.
            pass

    @staticmethod
    def _is_truthy(raw) -> bool:
        token = str(raw or "").strip().lower()
        return token in {"1", "true", "yes", "on"}

    @staticmethod
    def _int_setting(raw, default: int, *, minimum: int = 0, maximum: int = 86400) -> int:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = int(default)
        return max(minimum, min(maximum, value))

    @staticmethod
    def _parse_setting_timestamp(raw) -> datetime | None:
        token = str(raw or "").strip()
        if not token:
            return None
        try:
            parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(token, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _configured_tts_provider(settings: dict[str, str] | None) -> str:
        values = settings or {}
        provider = str(
            values.get("ai_tts_provider")
            or values.get("tts_provider")
            or "local-qwen-tts"
        ).strip().lower()
        if provider not in {"local-qwen-tts", "edge-tts", "omnivoice"}:
            return "local-qwen-tts"
        return provider

    def _station_settings(self) -> dict[str, str]:
        return SettingsRepository(self.conn).get_station(self.station_id)

    def _station_name(self) -> str:
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM stations WHERE id=? LIMIT 1", (int(self.station_id),))
        row = cur.fetchone()
        return str(row["name"] or f"Station {self.station_id}") if row else f"Station {self.station_id}"

    def _ensure_ai_prefetch_running(self, settings: dict[str, str] | None = None) -> None:
        if not self._is_truthy((settings or {}).get("ai_host_enabled", "false")):
            return
        try:
            from app.services.ai_prefetch import get_ai_prefetch

            result = get_ai_prefetch().ensure_running(self.station_id)
            if bool(result.get("restarted", False)):
                _log.warning("AI prefetch restarted for station %d", self.station_id)
        except Exception:
            _log.warning("AI prefetch health check failed", exc_info=True)

    def _probe_and_store_track_duration(self, track_id: int) -> float:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(file_path, '') AS file_path, COALESCE(duration, 0.0) AS duration "
            "FROM tracks WHERE id=? LIMIT 1",
            (int(track_id),),
        )
        row = cur.fetchone()
        if not row:
            return 0.0
        current_duration = float(row["duration"] or 0.0)
        if current_duration > 0.0:
            return current_duration
        resolved = resolve_runtime_media_path(str(row["file_path"] or "").strip())
        if not resolved or "://" in resolved:
            return 0.0
        candidate = Path(resolved)
        if not candidate.is_file():
            return 0.0
        try:
            from app.audio import audio_processing

            duration = float(
                audio_processing.probe_duration(
                    str(candidate),
                    timeout_seconds=5.0,
                )
            )
        except Exception:
            _log.warning("Could not probe duration for track %d", int(track_id), exc_info=True)
            return 0.0
        if duration <= 0.0:
            return 0.0
        cur.execute("UPDATE tracks SET duration=? WHERE id=?", (duration, int(track_id)))
        self.conn.commit()
        return duration

    def _queue_has_dedupe_key(self, dedupe_key: str) -> bool:
        row = self.queue_repo.find_by_dedupe_key(
            self.station_id,
            dedupe_key,
            statuses=("pending", "playing", "done"),
        )
        return row is not None

    def _current_playing_remaining_seconds(self) -> float:
        import datetime

        playing = self.queue_repo.current_playing(self.station_id)
        if not playing:
            return 0.0

        duration = float(playing["duration"] or 0.0)
        if duration <= 0.0:
            return 0.0

        started_at_str = playing["started_at"]
        if not started_at_str:
            return 0.0
        try:
            started_at = datetime.datetime.strptime(
                str(started_at_str), "%Y-%m-%d %H:%M:%S"
            )
        except (ValueError, TypeError):
            try:
                started_at = datetime.datetime.fromisoformat(str(started_at_str))
            except (ValueError, TypeError):
                return 0.0

        elapsed = (
            datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            - started_at
        ).total_seconds()
        return max(0.0, duration - elapsed)

    def _allow_inline_ai_generation(self, ai, settings: dict[str, str] | None = None) -> bool:
        # Prefetch owns normal intro generation. Worker falls back to inline only
        # when the fast host is already hot and there is enough playout buffer left.
        if ai is None:
            return False
        if str((settings or {}).get("ai_tts_provider", "edge-tts") or "edge-tts").strip().lower() in {
            "local",
            "local-qwen-tts",
            "omnivoice",
            "omni",
            "omnivoice-experimental",
            "qwen-tts",
        }:
            return False
        status_getter = getattr(ai, "get_load_status", None)
        if callable(status_getter):
            try:
                status = status_getter(settings=settings)
            except Exception:
                status = {}
            if not bool(status.get("llm_loaded", False)):
                return False
            if self._current_playing_remaining_seconds() < 20.0:
                return False
        return True

    def _next_pending_ai_target(self):
        rows = list(self.queue_repo.list_active_ordered(self.station_id))
        for idx, row in enumerate(rows):
            status = str(row["status"] or "").strip().lower()
            if status != "pending":
                continue
            track_type = str(row["track_type"] or "music").strip().lower() or "music"
            if track_type != "music":
                continue
            prev = rows[idx - 1] if idx > 0 else None
            if prev is not None:
                prev_status = str(prev["status"] or "").strip().lower()
                prev_type = str(prev["track_type"] or "music").strip().lower() or "music"
                if prev_status in {"pending", "playing"} and prev_type == "announcement":
                    continue
            return row
        return None

    def _enqueue_ai_announcement(self, announcement, *, before_item_id: int, dedupe_key: str) -> bool:
        audio_path = str(getattr(announcement, "audio_path", "") or "").strip()
        if not audio_path or not Path(audio_path).exists():
            return False
        duration = float(getattr(announcement, "duration_seconds", 0.0) or 0.0)
        track_id = TrackRepository(self.conn).upsert_generated_announcement(
            station_id=self.station_id,
            title=str(getattr(announcement, "title", "AI Announcement") or "AI Announcement"),
            file_path=audio_path,
            duration=duration,
            artist=str(getattr(announcement, "artist", "AI Host") or "AI Host"),
        )
        _item_id, created = self.queue_repo.insert_before_item_or_get_existing(
            station_id=self.station_id,
            before_item_id=int(before_item_id),
            track_id=track_id,
            dedupe_key=dedupe_key,
            dedupe_statuses=("pending", "playing", "done"),
        )
        if created:
            self._broadcast_worker_state(include_queue=True)
        return bool(created)

    def _maybe_prepare_ai_queue(self) -> bool:
        settings = self._station_settings()
        if not self._is_truthy(settings.get("ai_host_enabled", "false")):
            return False
        self._ensure_ai_prefetch_running(settings)

        target = self._next_pending_ai_target()
        if not target:
            return False

        target_item_id = int(target["id"])
        station_name = self._station_name()
        now_utc = datetime.now(timezone.utc)
        target_title = str(target["title"] or "").strip()

        # Track intros directly protect the next song from dead air, so they take
        # priority over station IDs whenever both are due.
        if target_title:
            intro_key = f"ai-track-intro:{target_item_id}"
            if not self._queue_has_dedupe_key(intro_key):
                announcement = self._find_prefetched_announcement(intro_key, settings=settings)
                if announcement is None:
                    try:
                        ai = self._get_ai_service()
                        if self._allow_inline_ai_generation(ai, settings=settings):
                            announcement = ai.generate_track_intro_announcement(
                                station_id=self.station_id,
                                station_name=station_name,
                                title=target_title,
                                artist=str(target["artist"] or "").strip(),
                                settings=settings,
                                dedupe_key=intro_key,
                            )
                    except Exception:
                        _log.warning("AI track intro generation failed", exc_info=True)

                if announcement and self._enqueue_ai_announcement(
                    announcement,
                    before_item_id=target_item_id,
                    dedupe_key=intro_key,
                ):
                    return True

        station_id_interval = self._int_setting(
            settings.get("ai_station_id_interval", "1800"),
            1800,
            minimum=0,
            maximum=86400,
        )
        last_station_id_at = self._parse_setting_timestamp(settings.get("_ai_last_station_id_at", ""))

        if station_id_interval > 0 and (
            last_station_id_at is None
            or now_utc - last_station_id_at >= timedelta(seconds=station_id_interval)
        ):
            station_id_key = f"ai-station-id:{target_item_id}"
            if not self._queue_has_dedupe_key(station_id_key):
                announcement = self._find_prefetched_announcement(station_id_key, settings=settings)
                if announcement is None:
                    try:
                        ai = self._get_ai_service()
                        if self._allow_inline_ai_generation(ai, settings=settings):
                            announcement = ai.generate_station_id_announcement(
                                station_id=self.station_id,
                                station_name=station_name,
                                settings=settings,
                                dedupe_key=station_id_key,
                            )
                    except Exception:
                        _log.warning("AI host service unavailable", exc_info=True)

                if announcement and self._enqueue_ai_announcement(
                    announcement,
                    before_item_id=target_item_id,
                    dedupe_key=station_id_key,
                ):
                    SettingsRepository(self.conn).upsert_station(
                        self.station_id,
                        {"_ai_last_station_id_at": now_utc.isoformat(timespec="seconds")},
                    )
                    return True

        return False

    def _get_ai_service(self):
        """Get AI service, trying fast first then fallback."""
        try:
            try:
                from app.services.ai_host_fast import get_ai_host_fast
                return get_ai_host_fast()
            except Exception:
                from app.services.ai_host import get_ai_host
                return get_ai_host()
        except Exception:
            _log.warning("AI host service unavailable", exc_info=True)
            return None

    def _find_prefetched_announcement(self, dedupe_key: str, *, settings: dict[str, str] | None = None):
        """Find a pre-generated announcement in the AI cache by dedupe key."""
        from app.services.ai_host import CACHE_DIR
        from app.services.ai_host import AIAnnouncement
        from app.services.ai_cache_index import get_announcement_cache_index

        expected_provider = self._configured_tts_provider(settings)
        payload = get_announcement_cache_index(CACHE_DIR).lookup(
            dedupe_key,
            expected_tts_provider=expected_provider,
        )
        if payload is None:
            return None
        audio_path = str(payload.get("audio_path", "") or "")
        return AIAnnouncement(
            cache_key=str(payload.get("cache_key", "") or ""),
            station_id=int(payload.get("station_id", 1) or 1),
            station_name=str(payload.get("station_name", "") or ""),
            announcement_type=str(payload.get("announcement_type", "") or ""),
            title=str(payload.get("title", "") or ""),
            artist=str(payload.get("artist", "") or ""),
            text=str(payload.get("text", "") or ""),
            audio_path=audio_path,
            duration_seconds=float(payload.get("duration_seconds", 0) or 0),
            llm_provider=str(payload.get("llm_provider", "prefetch") or "prefetch"),
            tts_provider=str(payload.get("tts_provider", "prefetch") or "prefetch"),
            generated_at=str(payload.get("generated_at", "") or ""),
            dedupe_key=str(payload.get("dedupe_key", "") or ""),
        )

    def _preload_upcoming_ai_announcements(self) -> None:
        """Pre-generate AI announcements for upcoming tracks and add to queue."""
        settings = self._station_settings()
        if not self._is_truthy(settings.get("ai_host_enabled", "false")):
            return
        preload_count = self._int_setting(settings.get("ai_preload_count", "5"), 5, minimum=1, maximum=20)
        if preload_count < 1:
            return
        try:
            from app.services.ai_host import get_ai_host
            ai = get_ai_host()
        except Exception:
            _log.warning("AI host service unavailable for preloading", exc_info=True)
            return
        rows = list(self.queue_repo.list_active_ordered(self.station_id))
        upcoming_music = []
        for row in rows:
            status = str(row["status"] or "").strip().lower()
            if status != "pending":
                continue
            track_type = str(row["track_type"] or "music").strip().lower() or "music"
            if track_type != "music":
                continue
            title = str(row["title"] or "").strip()
            artist = str(row["artist"] or "").strip()
            if title:
                upcoming_music.append({"id": int(row["id"]), "title": title, "artist": artist})
            if len(upcoming_music) >= preload_count:
                break
        if not upcoming_music:
            return
        station_name = self._station_name()
        for track_info in upcoming_music:
            intro_key = f"ai-track-intro:{track_info['id']}"
            if self._queue_has_dedupe_key(intro_key):
                continue
            try:
                announcement = ai.generate_track_intro_announcement(
                    station_id=self.station_id,
                    station_name=station_name,
                    title=track_info["title"],
                    artist=track_info["artist"],
                    settings=settings,
                    dedupe_key=intro_key,
                )
                if announcement:
                    self._enqueue_ai_announcement(
                        announcement,
                        before_item_id=track_info["id"],
                        dedupe_key=intro_key,
                    )
                    _log.info(f"Preloaded AI intro for: {track_info['title']}")
            except Exception:
                _log.warning(f"Failed to preload AI intro for: {track_info['title']}", exc_info=True)

    def _set_playout_state(
        self, source: str, item_id: int | None, *, reason: str
    ) -> None:
        """Record a transition while tolerating older test/plugin state adapters."""
        try:
            self.playout_state.set_current(
                self.station_id, source, item_id, reason=reason
            )
        except TypeError as exc:
            if "reason" not in str(exc):
                raise
            self.playout_state.set_current(self.station_id, source, item_id)

    def _record_ai_broadcast(self, track_id: int, event_type: str) -> None:
        """Best-effort audit evidence that can never block the playout path."""
        try:
            if self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtai_ai_drafts'"
            ).fetchone() is None:
                return
            drafts = self.conn.execute(
                "SELECT id, channel_uuid, task, provider, projection_sha256, status "
                "FROM rtai_ai_drafts WHERE cached_track_id=? "
                "AND status IN ('approved_cached','broadcast_queued','broadcast_started')",
                (int(track_id),),
            ).fetchall()
            for draft in drafts:
                next_status = (
                    "broadcast_started" if event_type == "broadcast_started" else "broadcast_completed"
                )
                with self.conn:
                    self.conn.execute(
                        "UPDATE rtai_ai_drafts SET status=?, "
                        "broadcast_at=COALESCE(broadcast_at, CURRENT_TIMESTAMP) WHERE id=?",
                        (next_status, int(draft["id"])),
                    )
                    self.conn.execute(
                        "INSERT INTO rtai_ai_audit(channel_uuid, draft_id, event_type, task, "
                        "provider, projection_sha256, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(draft["channel_uuid"]),
                            int(draft["id"]),
                            event_type,
                            str(draft["task"]),
                            str(draft["provider"]),
                            str(draft["projection_sha256"]),
                            json.dumps(
                                {"track_id": int(track_id), "station_id": self.station_id},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
        except Exception:
            _log.warning("AI broadcast audit write failed", exc_info=True)

    def _play_managed_item(
        self,
        source: str,
        item_id: int,
        track_id: int,
        mark_playing,
        mark_done,
        mark_failed,
        *,
        auto_done: bool = True,
    ) -> dict:
        mark_playing(item_id)
        self._set_playout_state(source, item_id, reason=f"{source}_start")
        track_uri, stream_title, stream_artist, stream_album, track_type = self._track_runtime_fields(track_id)
        if not track_uri:
            mark_failed(item_id)
            self._set_playout_state(
                "none", None, reason=f"{source}_track_missing"
            )
            return {"source": source, "reason": "track_missing"}

        # ── AI Host: Play announcement before track ──
        try:
            if self.runtime_registry:
                self._start_runtime_station(
                    self.station_id, track_uri,
                    stream_title=stream_title, stream_artist=stream_artist,
                    stream_album=stream_album,
                    track_type=track_type,
                    crossfade_seconds=self._default_crossfade_seconds(),
                )
            self._record_ai_broadcast(track_id, "broadcast_started")
            self._broadcast_worker_state(include_queue=True, include_track=True)
            if auto_done:
                mark_done(item_id)
                self._set_playout_state(
                    "none", None, reason=f"{source}_auto_done"
                )
            return {"source": source, "input_uri": track_uri, "item_id": item_id}
        except Exception as exc:
            if self._is_transient_output_failure(exc):
                self._restore_managed_item_pending(source, item_id)
                self._set_playout_state(
                    "none", None, reason=f"{source}_output_retry"
                )
                raise
            mark_failed(item_id)
            self._set_playout_state("none", None, reason=f"{source}_failed")
            raise

    def _play_host_track(self, host_item_id: int, track_id: int) -> dict:
        self._finish_playing_queue_item()  # preempt current automation track
        self._set_playout_state("host", host_item_id, reason="host_start")
        track_uri, stream_title, stream_artist, stream_album, track_type = self._track_runtime_fields(
            track_id
        )
        if not track_uri:
            self.program_queue_repo.pop_item(host_item_id)
            self._set_playout_state("none", None, reason="host_track_missing")
            return {"source": "host", "reason": "track_missing"}
        if self.runtime_registry:
            self._start_runtime_station(
                self.station_id,
                track_uri,
                stream_title=stream_title,
                stream_artist=stream_artist,
                stream_album=stream_album,
                track_type=track_type,
                crossfade_seconds=self._default_crossfade_seconds(),
            )
        self._broadcast_worker_state(include_queue=True, include_track=True)
        return {"source": "host", "input_uri": track_uri}

    def _advance_host_track(self) -> bool:
        """Check if the current host track has finished playing."""
        current = self.playout_state.get_current(self.station_id)
        if current["source"] != "host" or current["item_id"] is None:
            return False
        if self.runtime_registry:
            rt_status = self.runtime_registry.status(self.station_id)
            if self._runtime_playback_alive(rt_status):
                return False  # still playing
        # Track finished — pop from host queue and reset state
        self.program_queue_repo.pop_item(int(current["item_id"]))
        self._set_playout_state("none", None, reason="host_track_complete")
        self._broadcast_worker_state(include_queue=True)
        return True

    def _complete_queue_item(self, playing) -> None:
        """Mark queue item done and update track play statistics."""
        self.queue_repo.mark_done(int(playing["id"]))
        self._set_playout_state("none", None, reason="queue_track_complete")
        track_id = int(playing["track_id"] or 0)
        if track_id > 0:
            TrackRepository(self.conn).mark_played(track_id)
            try:
                delivered_variants = []
                if self.runtime_registry:
                    runtime_status = self.runtime_registry.status(self.station_id) or {}
                    branch_health = dict(runtime_status.get("branch_health") or {})
                    for output in runtime_status.get("extra_icecast_mounts") or []:
                        if not isinstance(output, dict):
                            continue
                        branch = str(output.get("branch") or "")
                        if not bool(branch_health.get(branch, False)):
                            continue
                        mount = str(output.get("mount") or "")
                        delivered_variants.append(
                            {
                                "mount": mount,
                                "quality": (
                                    mount.rsplit("-", 1)[-1] if "-" in mount else ""
                                ),
                                "codec_profile": str(
                                    output.get("codec_profile") or ""
                                ),
                                "bitrate_kbps": int(
                                    output.get("bitrate_kbps") or 0
                                ),
                            }
                        )
                active_show = self._get_active_show_session() or {}
                program_name = str(
                    active_show.get("show_name")
                    or active_show.get("name")
                    or active_show.get("title")
                    or ""
                )
                presenter = str(
                    active_show.get("presenter")
                    or active_show.get("host_name")
                    or active_show.get("operator_name")
                    or ""
                )
                MusicUsageService(self.conn).record_completed_play(
                    station_id=self.station_id,
                    track_id=track_id,
                    queue_item_id=int(playing["id"]),
                    started_at=playing["started_at"],
                    finished_at=datetime.now(timezone.utc),
                    program_name=program_name,
                    presenter=presenter,
                    delivered_variants=delivered_variants,
                )
            except Exception:
                # Playout remains live; the append-only writer is retried by
                # the legacy acknowledgement path or an operator export run.
                _log.exception("Could not persist music-use record for queue item %s", playing["id"])
            self._record_ai_broadcast(track_id, "broadcast_completed")
        self._broadcast_worker_state(include_queue=True)

    @staticmethod
    def _runtime_playback_alive(rt_status: dict | None) -> bool:
        if not isinstance(rt_status, dict):
            return False
        branch_health = rt_status.get("branch_health")
        if isinstance(branch_health, dict):
            required_outputs = rt_status.get("required_outputs")
            if isinstance(required_outputs, dict):
                required = [
                    str(branch)
                    for branch, enabled in required_outputs.items()
                    if bool(enabled)
                ]
                if required and any(bool(branch_health.get(branch, False)) for branch in required):
                    return True
            elif any(bool(value) for value in branch_health.values()):
                return True
        if "program_running" in rt_status:
            return bool(rt_status.get("program_running", False))
        return bool(rt_status.get("running", False))

    @staticmethod
    def _same_runtime_uri(left: str, right: str) -> bool:
        def normalized(value: str) -> str:
            token = str(value or "").strip().replace("\\", "/")
            if token.lower().startswith("file:///"):
                token = unquote(token[8:])
            return token.rstrip("/").casefold()

        return bool(normalized(left)) and normalized(left) == normalized(right)

    def _runtime_playback_matches(
        self, rt_status: dict | None, expected_uri: str
    ) -> bool:
        if not self._runtime_playback_alive(rt_status) or not isinstance(
            rt_status, dict
        ):
            return False
        if "program_running" in rt_status and not bool(
            rt_status.get("program_running", False)
        ):
            return False
        active_uri = str(rt_status.get("active_input_uri") or "").strip()
        if not active_uri:
            # Older runtime adapters do not expose the active URI.
            return True
        return self._same_runtime_uri(active_uri, expected_uri)

    @staticmethod
    def _runtime_program_running(rt_status: dict | None) -> bool:
        if not isinstance(rt_status, dict):
            return False
        return bool(
            rt_status.get("program_running", rt_status.get("running", False))
        )

    @staticmethod
    def _row_value(row, key: str, default=None):
        if row is None:
            return default
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    def _ads_enabled(self) -> bool:
        conn = getattr(self, "conn", None)
        if conn is None:
            return False
        try:
            return station_ads_enabled(conn, self.station_id)
        except Exception:
            _log.exception(
                "Could not evaluate ad policy for station_id=%s; ads disabled",
                self.station_id,
            )
            return False

    def _fail_ad_policy_violation(
        self, item, reason: str = "ads_disabled_for_station"
    ) -> None:
        item_id = int(self._row_value(item, "id", 0) or 0)
        status = str(self._row_value(item, "status", "") or "").lower()
        if item_id <= 0:
            return
        self.ad_repo.mark_failed(item_id)
        if status == "playing":
            self._set_playout_state("none", None, reason=reason)
        _log.warning(
            "Blocked ad item station_id=%s item_id=%s: %s",
            self.station_id,
            item_id,
            reason,
        )
        self._broadcast_worker_state(
            include_queue=True, include_track=status == "playing"
        )

    def _fail_disabled_active_ads(self) -> int:
        if self._ads_enabled():
            return 0
        list_active = getattr(self.ad_repo, "list_active", None)
        if callable(list_active):
            try:
                rows = list(list_active(self.station_id, limit=100))
            except TypeError:
                rows = list(list_active(self.station_id))
        else:
            rows = []
            for name in ("current_playing", "next_due"):
                getter = getattr(self.ad_repo, name, None)
                row = getter(self.station_id) if callable(getter) else None
                if row is not None:
                    rows.append(row)
        failed = 0
        seen: set[int] = set()
        for row in rows:
            item_id = int(self._row_value(row, "id", 0) or 0)
            if item_id <= 0 or item_id in seen:
                continue
            seen.add(item_id)
            self._fail_ad_policy_violation(row)
            failed += 1
        return failed

    def _next_due_ad_if_allowed(self):
        if not self._ads_enabled():
            self._fail_disabled_active_ads()
            return None
        return self.ad_repo.next_due(self.station_id)

    def _restart_playing_ad_item_if_runtime_mismatched(self, playing) -> bool:
        if not self.runtime_registry or not playing:
            return False
        if not self._ads_enabled():
            self._fail_ad_policy_violation(playing)
            self._start_continuity_fallback(
                reason="ads_disabled_for_station", failed_source="ads"
            )
            return True
        item_id = int(self._row_value(playing, "id", 0) or 0)
        track_id = int(self._row_value(playing, "track_id", 0) or 0)
        track_uri, title, artist, album, track_type = self._track_runtime_fields(track_id)
        status = self.runtime_registry.status(self.station_id)
        if track_uri and self._runtime_playback_matches(status, track_uri):
            return False
        allowed, suppression_reason = self._restart_attempt_allowed(
            "ads", item_id
        )
        if not allowed:
            self.ad_repo.mark_failed(item_id)
            self._set_playout_state(
                "none", None, reason=f"ad_{suppression_reason}"
            )
            self._start_continuity_fallback(
                reason="restart_suppressed", failed_source="ads"
            )
            return True
        if not track_uri:
            self.ad_repo.mark_failed(item_id)
            self._set_playout_state("none", None, reason="ad_track_missing")
            self._start_continuity_fallback(
                reason="track_missing", failed_source="ads"
            )
            return True
        try:
            self._start_runtime_station(
                self.station_id,
                track_uri,
                stream_title=title,
                stream_artist=artist,
                stream_album=album,
                track_type=track_type,
                crossfade_seconds=0.0,
            )
            self.ad_repo.mark_playing(item_id)
            self._set_playout_state("ads", item_id, reason="ad_runtime_recovered")
            self._broadcast_worker_state(include_queue=True, include_track=True)
        except Exception:
            self.ad_repo.mark_failed(item_id)
            self._set_playout_state(
                "none", None, reason="ad_runtime_recovery_failed"
            )
            self._start_continuity_fallback(
                reason="source_start_failed", failed_source="ads"
            )
        return True

    def _advance_playing_ad_item(self) -> bool:
        current_playing = getattr(self.ad_repo, "current_playing", None)
        playing = (
            current_playing(self.station_id)
            if callable(current_playing)
            else None
        )
        if not playing:
            return False
        if not self._ads_enabled():
            self._fail_ad_policy_violation(playing)
            self._start_continuity_fallback(
                reason="ads_disabled_for_station", failed_source="ads"
            )
            return True

        started_raw = self._row_value(playing, "started_at", "")
        if not started_raw:
            return False
        try:
            started_at = datetime.strptime(
                str(started_raw), "%Y-%m-%d %H:%M:%S"
            )
        except (TypeError, ValueError):
            try:
                started_at = datetime.fromisoformat(str(started_raw))
            except (TypeError, ValueError):
                return False
        elapsed = (
            datetime.now(timezone.utc).replace(tzinfo=None) - started_at
        ).total_seconds()
        duration = float(self._row_value(playing, "duration", 0.0) or 0.0)
        if duration > 0 and elapsed < duration:
            if elapsed >= 2.0:
                self._restart_playing_ad_item_if_runtime_mismatched(playing)
            return False
        if duration <= 0 and elapsed < 90.0 and self.runtime_registry:
            status = self.runtime_registry.status(self.station_id)
            if self._runtime_playback_alive(status):
                return False
        item_id = int(self._row_value(playing, "id", 0) or 0)
        track_id = int(self._row_value(playing, "track_id", 0) or 0)
        self.ad_repo.mark_done(item_id)
        if track_id > 0 and getattr(self, "conn", None) is not None:
            TrackRepository(self.conn).mark_played(track_id)
        self._set_playout_state("none", None, reason="ad_complete")
        self._broadcast_worker_state(include_track=True)
        return True

    @staticmethod
    def _parse_id_list(raw) -> list[int]:
        result: list[int] = []
        for token in str(raw or "").replace(";", ",").split(","):
            try:
                value = int(token.strip())
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in result:
                result.append(value)
        return result

    def _get_hourly_ad_settings(self) -> dict:
        settings = SettingsRepository(self.conn).get_station(self.station_id)
        try:
            interval = max(
                1, int(float(settings.get("hourly_ad_interval_minutes", 60)))
            )
        except (TypeError, ValueError):
            interval = 60
        return {
            "enabled": ads_enabled_from_settings(settings),
            "interval_minutes": interval,
            "ad_count": self._parse_id_list(
                settings.get("hourly_ad_track_ids", "")
            ),
        }

    def _ensure_hourly_ad_break(self) -> int:
        settings = self._get_hourly_ad_settings()
        if not bool(settings.get("enabled")):
            return 0
        # Existing pending/playing rows already provide the next break.
        if list(self.ad_repo.list_active(self.station_id, limit=1)):
            return 0
        track_ids = [int(value) for value in settings.get("ad_count", [])]
        if not track_ids:
            return 0
        interval = int(settings.get("interval_minutes") or 60)
        due = datetime.now(timezone.utc) + timedelta(minutes=interval)
        due_at = due.strftime("%Y-%m-%d %H:%M:%S")
        bucket = due.strftime("%Y%m%d%H%M")
        for priority, track_id in enumerate(track_ids):
            self.ad_repo.enqueue(
                self.station_id,
                track_id,
                due_at,
                priority=-priority,
                dedupe_key=f"hourly:{self.station_id}:{bucket}:{track_id}",
            )
        return len(track_ids)

    def _restart_attempt_allowed(
        self, source: str, item_id: int
    ) -> tuple[bool, str]:
        now = time.monotonic()
        key = (int(self.station_id), str(source or ""), int(item_id))
        state = _RESTART_SUPPRESSION.get(key)
        if not state:
            _RESTART_SUPPRESSION[key] = {
                "attempts": 1,
                "next_allowed": now + _RESTART_COOLDOWN_SEC,
                "reason": "",
            }
            return True, ""
        if int(state.get("attempts") or 0) >= _MAX_RESTART_ATTEMPTS_PER_ITEM:
            state["reason"] = "restart_limit_reached"
            return False, "restart_limit_reached"
        if now < float(state.get("next_allowed") or 0.0):
            state["reason"] = "restart_cooldown_active"
            return False, "restart_cooldown_active"
        state["attempts"] = int(state.get("attempts") or 0) + 1
        state["next_allowed"] = now + _RESTART_COOLDOWN_SEC
        state["reason"] = ""
        return True, ""

    def _restart_playing_queue_item_if_runtime_mismatched(
        self,
        playing,
        *,
        start_offset_seconds: float = 0.0,
    ) -> bool:
        if not self.runtime_registry or not playing:
            return False
        item_id = int(playing["id"])
        track_id = int(playing["track_id"] or 0)
        track_uri, title, artist, album, track_type = self._track_runtime_fields(track_id)
        if not track_uri:
            self.queue_repo.mark_failed(item_id)
            self._set_playout_state("none", None, reason="manual_track_missing")
            return True
        status = self.runtime_registry.status(self.station_id)
        if self._runtime_playback_matches(status, track_uri):
            return False
        allowed, suppression_reason = self._restart_attempt_allowed(
            "manual", item_id
        )
        if not allowed:
            if suppression_reason == "restart_cooldown_active":
                # The runtime status can lag immediately after a successful
                # seamless input switch. Keep the queue item playing during
                # that observation window; failing it here cuts audio early.
                return False
            self.queue_repo.mark_failed(item_id)
            self._set_playout_state(
                "none", None, reason=f"manual_{suppression_reason}"
            )
            _log.error(
                "Suppressed repeated restart for station_id=%s queue item %s: %s",
                self.station_id,
                item_id,
                suppression_reason,
            )
            return True
        try:
            self._start_runtime_station(
                self.station_id,
                track_uri,
                stream_title=title,
                stream_artist=artist,
                stream_album=album,
                track_type=track_type,
                crossfade_seconds=0.0,
                start_offset_seconds=max(0.0, float(start_offset_seconds or 0.0)),
            )
            self._set_playout_state(
                "manual", item_id, reason="manual_runtime_recovered"
            )
            self._broadcast_worker_state(include_queue=True, include_track=True)
            return True
        except Exception:
            self.queue_repo.mark_failed(item_id)
            self._set_playout_state(
                "none", None, reason="manual_runtime_recovery_failed"
            )
            _log.exception(
                "Failed to recover station_id=%s queue item %s",
                self.station_id,
                item_id,
            )
            return True

    def _advance_playing_queue_item(self) -> bool:
        """Check if current playing queue item's duration has elapsed.
        If so, mark it done and return True so the next track can start."""
        import datetime

        playing = self.queue_repo.current_playing(self.station_id)
        if not playing:
            return False

        started_at_str = playing["started_at"]
        if not started_at_str:
            return False

        # Parse started_at timestamp (UTC from SQLite CURRENT_TIMESTAMP)
        try:
            started_at = datetime.datetime.strptime(
                str(started_at_str), "%Y-%m-%d %H:%M:%S"
            )
        except (ValueError, TypeError):
            try:
                started_at = datetime.datetime.fromisoformat(str(started_at_str))
            except (ValueError, TypeError):
                return False

        elapsed = (
            datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            - started_at
        ).total_seconds()
        duration = float(playing["duration"] or 0.0)

        # Only subtract crossfade when both current AND next tracks are
        # music, because crossfade only applies to music→music transitions.
        # For jingle→music or music→jingle the runtime does a hard restart,
        # so we must let the current track play its full duration.
        crossfade = 0.0
        current_type = str(playing["track_type"] or "music").strip().lower()
        next_type = ""
        if current_type in {"music", "jingle"}:
            next_pending = self.queue_repo.next_pending(self.station_id)
            if next_pending:
                try:
                    next_type = str(next_pending["track_type"] or "music").strip().lower()
                except (KeyError, IndexError):
                    next_type = "music"
                if (
                    next_type in {"music", "jingle"}
                    and current_type in {"music", "jingle"}
                ):
                    configured_crossfade = self._default_crossfade_seconds()
                    if configured_crossfade > 0.0:
                        crossfade = min(0.25, configured_crossfade)
                    if current_type == "music" and next_type == "music":
                        crossfade = configured_crossfade
        advance_at = max(0.0, duration - crossfade)

        # A healthy encoder process can still be rendering the wrong file after
        # a stale queue transition. Process liveness alone must not freeze the
        # queue indefinitely; reconcile its active URI with the playing row.
        #
        # Only treat a divergent active URI as a fault while the track should
        # still be playing. Short jingles (e.g. the 1s sweeper) finish between
        # worker polls; once elapsed time has passed the track's natural end
        # (advance_at) the runtime correctly moves on, so a URI "mismatch" is
        # expected and the item must be allowed to complete rather than being
        # restarted/failed.
        if self.runtime_registry and elapsed < advance_at:
            rt_status = self.runtime_registry.status(self.station_id)
            track_uri, _title, _artist, _album, _track_type = self._track_runtime_fields(
                int(playing["track_id"] or 0)
            )
            if (
                track_uri
                and self._runtime_playback_alive(rt_status)
                and not self._runtime_playback_matches(rt_status, track_uri)
            ):
                if current_type == "jingle" and duration <= 3.0:
                    # A very short station ID can finish between worker polls.
                    _log.info(
                        "Completing short jingle queue item %s after runtime advanced",
                        int(playing["id"]),
                    )
                    self._complete_queue_item(playing)
                    return True
                return self._restart_playing_queue_item_if_runtime_mismatched(
                    playing,
                    start_offset_seconds=min(
                        max(0.0, elapsed),
                        max(0.0, duration - 0.25),
                    ),
                )

        # ── Safety: absolute max timeout per track ────────────
        # Prevents tracks from being stuck forever (hung process,
        # wrong metadata, etc.). Music is capped at 30 minutes; known-duration
        # long-form programming gets a duration-relative safety window.
        timeout_track_type = str(playing["track_type"] or "music").strip().lower()
        if duration > 0:
            duration_guard = duration * 1.5 + 60.0
            _max = (
                duration_guard
                if timeout_track_type in {"announcement", "podcast", "spoken_word"}
                else min(duration_guard, 1800.0)
            )
        else:
            _max = 1800.0
        if elapsed >= _max:
            _log.warning(
                "Track exceeded max allowed time (%.1f/%.1fs), "
                "force-advancing queue item %d",
                elapsed, _max, int(playing["id"]),
            )
            self._complete_queue_item(playing)
            return True

        if duration <= 0:
            # Unknown duration — check if the runtime process has already
            # exited.  If so, mark done to avoid getting stuck forever.
            if self.runtime_registry:
                rt_status = self.runtime_registry.status(self.station_id)
                if not self._runtime_playback_alive(rt_status):
                    self._complete_queue_item(playing)
                    return True
            return False

        if elapsed >= advance_at:
            music_crossfade_due = (
                current_type in {"music", "jingle"}
                and next_type in {"music", "jingle"}
                and crossfade > 0.0
            )
            if self.runtime_registry and not music_crossfade_due:
                rt_status = self.runtime_registry.status(self.station_id)
                if self._runtime_playback_alive(rt_status):
                    # Only wait if the runtime is still rendering THIS track.
                    # A jingle that has finished is immediately followed by the
                    # next item, so the runtime stays alive; if it is playing a
                    # different source the current item has run its course and
                    # must be completed rather than hanging in "playing" forever.
                    current_track_uri, _, _, _, _ = self._track_runtime_fields(
                        int(playing["track_id"] or 0)
                    )
                    if current_track_uri and self._runtime_playback_matches(
                        rt_status, current_track_uri
                    ):
                        return False
            self._complete_queue_item(playing)
            return True

        # If the runtime dies or switches source before the expected end, retry
        # the same item once. Advancing would silently skip the listener's song.
        if self.runtime_registry:
            rt_status = self.runtime_registry.status(self.station_id)
            if not self._runtime_playback_alive(rt_status):
                if current_type == "jingle" and duration <= 3.0:
                    _log.info(
                        "Completing short jingle queue item %s after runtime ended",
                        int(playing["id"]),
                    )
                    self._complete_queue_item(playing)
                    return True
                _log.warning(
                    "Runtime dead before duration elapsed (%.1f/%.1fs), recovering queue item %d",
                    elapsed, duration, int(playing["id"]),
                )
                self._restart_playing_queue_item_if_runtime_mismatched(
                    playing,
                    start_offset_seconds=min(
                        max(0.0, elapsed),
                        max(0.0, duration - 0.25),
                    ),
                )
                return False

        return False

    def _finish_playing_queue_item(self):
        """Mark current playing queue item as done (used when another source preempts)."""
        playing = self.queue_repo.current_playing(self.station_id)
        if playing:
            self._complete_queue_item(playing)

    # ------------------------------------------------------------------
    # Sweeper / Auto-Jingle
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Startup Sound — play a jingle/sound when broadcasting begins
    # ------------------------------------------------------------------
    def _get_startup_sound_settings(self) -> dict:
        """Read startup-sound config from station_settings."""
        settings = SettingsRepository(self.conn).get_station(self.station_id)
        enabled = str(settings.get("startup_sound_enabled", "false")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        mode = str(settings.get("startup_sound_mode", "random") or "random")  # "random" | "specific"
        try:
            track_id = int(float(settings.get("startup_sound_track_id", "0")))
        except (TypeError, ValueError):
            track_id = 0
        return {"enabled": enabled, "mode": mode, "track_id": track_id}

    def _is_startup_sound_pending(self) -> bool:
        """Check if a startup sound is waiting to be played."""
        settings = SettingsRepository(self.conn).get_station(self.station_id)
        return str(settings.get("_startup_sound_pending", "false")).strip().lower() in {
            "1", "true", "yes", "on",
        }

    def _clear_startup_sound_pending(self) -> None:
        """Mark startup sound as played."""
        SettingsRepository(self.conn).upsert_station(
            self.station_id, {"_startup_sound_pending": "false"}
        )

    def _maybe_insert_startup_sound(self) -> bool:
        """If startup-sound is enabled and pending, insert the sound at queue front.
        Returns True if a startup sound was inserted."""
        if not self._is_startup_sound_pending():
            return False

        # A restart can preserve an automatic sweeper at the front of the
        # queue. Inserting another startup jingle ahead of it would create the
        # exact double-RadioTEDU-jingle failure operators reported. Treat the
        # preserved front jingle as satisfying the startup identity and clear
        # the one-shot flag without changing queue order.
        if self._next_pending_is_jingle():
            self._clear_startup_sound_pending()
            _log.info(
                "Startup sound suppressed for station %s because the preserved queue already starts with a jingle",
                self.station_id,
            )
            return False

        cfg = self._get_startup_sound_settings()
        if not cfg["enabled"]:
            self._clear_startup_sound_pending()
            return False

        # Pick the track to play
        track_id = 0
        if cfg["mode"] == "specific" and cfg["track_id"] > 0:
            # Verify the track exists and is active
            cur = self.conn.cursor()
            cur.execute(
                "SELECT id FROM tracks WHERE id=? AND is_active=1 AND COALESCE(file_path, '') <> ''",
                (cfg["track_id"],),
            )
            if cur.fetchone():
                track_id = cfg["track_id"]
            else:
                _log.warning("Startup sound: specific track %d not found/inactive, falling back to random", cfg["track_id"])

        if track_id <= 0:
            # Random jingle
            jingle = self._pick_random_jingle()
            if jingle:
                track_id = jingle["track_id"]

        if track_id <= 0:
            _log.warning("Startup sound: no suitable track found, skipping")
            self._clear_startup_sound_pending()
            return False

        # Insert at front of pending queue
        cur = self.conn.cursor()
        cur.execute(
            "SELECT MIN(position) as min_pos FROM queue_items "
            "WHERE station_id=? AND status='pending'",
            (self.station_id,),
        )
        row = cur.fetchone()
        min_pos = int(row["min_pos"] or 1) if row and row["min_pos"] is not None else 1
        insert_pos = max(1, min_pos - 1)

        cur.execute(
            "INSERT OR IGNORE INTO queue_items (station_id, track_id, position, status, dedupe_key) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (self.station_id, track_id, insert_pos, f"startup_sound:{track_id}"),
        )
        inserted = int(cur.rowcount or 0) > 0
        # Clear the one-shot flag in the same transaction as the queue insert.
        # A crash can no longer commit the jingle but leave it pending for a
        # second worker pass.
        cur.execute(
            "INSERT INTO station_settings (station_id, key, value) "
            "VALUES (?, '_startup_sound_pending', 'false') "
            "ON CONFLICT(station_id, key) DO UPDATE SET value='false'",
            (self.station_id,),
        )
        self.conn.commit()
        if inserted:
            _log.info("Startup sound: inserted track_id=%d at front of queue", track_id)
        return inserted

    def _get_sweeper_settings(self) -> dict:
        """Read sweeper config from station_settings."""
        settings = SettingsRepository(self.conn).get_station(self.station_id)
        enabled = str(settings.get("sweeper_enabled", "false")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        try:
            interval = max(1, int(float(settings.get("sweeper_interval", "2"))))
        except (TypeError, ValueError):
            interval = 2
        mode = str(settings.get("sweeper_mode", "random") or "random")
        interval_unit = str(settings.get("sweeper_interval_unit", "tracks") or "tracks").strip().lower()
        if interval_unit not in {"tracks", "minutes"}:
            interval_unit = "tracks"
        try:
            baseline_queue_id = max(0, int(settings.get("sweeper_baseline_queue_id", 0) or 0))
        except (TypeError, ValueError):
            baseline_queue_id = 0
        return {
            "enabled": enabled,
            "interval": interval,
            "interval_unit": interval_unit,
            "baseline_queue_id": baseline_queue_id,
            "mode": mode,
        }

    def _count_music_since_last_jingle(self) -> int:
        """Count how many music tracks have been played since the last jingle."""
        cur = self.conn.cursor()
        # Playback chronology, not enqueue chronology, is authoritative. A
        # just-in-time jingle receives a high queue id and is then followed by
        # older prefilled rows; ordering by id therefore froze the counter at
        # zero and stretched a three-song cadence to a whole refill batch.
        cur.execute(
            "SELECT q.id, COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status IN ('done', 'playing') "
            "ORDER BY COALESCE(q.started_at, q.finished_at, q.enqueued_at) DESC, "
            "q.id DESC LIMIT 50",
            (self.station_id,),
        )
        count = 0
        for row in cur.fetchall():
            tt = str(row["track_type"] or "music").strip().lower()
            if tt == "jingle":
                break  # found last jingle, stop counting
            if tt == "music":
                count += 1
        return count

    def _music_seconds_since_last_jingle(self, baseline_queue_id: int = 0) -> float:
        """Sum station music durations since the last jingle.

        A playing song contributes its full duration so a jingle is scheduled
        after the song that crosses the target. The song is never shortened.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.id, COALESCE(t.track_type, 'music') AS track_type, "
            "COALESCE(t.duration, 0.0) AS duration "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status IN ('done', 'playing') AND q.id>? "
            "ORDER BY COALESCE(q.started_at, q.finished_at, q.enqueued_at) DESC, "
            "q.id DESC LIMIT 500",
            (self.station_id, max(0, int(baseline_queue_id))),
        )
        seconds = 0.0
        for row in cur.fetchall():
            track_type = str(row["track_type"] or "music").strip().lower()
            if track_type == "jingle":
                break
            if track_type == "music":
                try:
                    seconds += max(0.0, float(row["duration"] or 0.0))
                except (TypeError, ValueError):
                    continue
        return seconds

    def _pick_random_jingle(self) -> dict | None:
        """Pick the next active jingle deterministically for this station only."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM tracks "
            "WHERE station_id=? "
            "AND LOWER(COALESCE(track_type, ''))='jingle' AND is_active=1 "
            "AND COALESCE(file_path, '') <> '' "
            "ORDER BY COALESCE(play_count, 0) ASC, "
            "CASE WHEN last_played_at IS NULL OR TRIM(last_played_at)='' THEN 0 ELSE 1 END ASC, "
            "last_played_at ASC, id ASC LIMIT 1",
            (self.station_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        return {"track_id": int(rows[0]["id"])}

    def _pick_random_ad(self) -> dict | None:
        """Pick the next active global ad deterministically for this station."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM tracks "
            "WHERE station_id=? "
            "AND LOWER(COALESCE(track_type, ''))='ad' AND is_active=1 "
            "AND COALESCE(file_path, '') <> '' "
            "ORDER BY COALESCE(play_count, 0) ASC, "
            "CASE WHEN last_played_at IS NULL OR TRIM(last_played_at)='' THEN 0 ELSE 1 END ASC, "
            "last_played_at ASC, id ASC LIMIT 1",
            (self.station_id,),
        )
        row = cur.fetchone()
        return {"track_id": int(row["id"])} if row else None

    def _remove_pending_jingles(self) -> int:
        """Remove all pending jingle items from the queue."""
        cur = self.conn.cursor()
        # Use a two-step approach to avoid SQLite self-referencing DELETE issues
        cur.execute(
            "SELECT qi.id FROM queue_items qi "
            "LEFT JOIN tracks t ON t.id = qi.track_id "
            "WHERE qi.station_id=? AND qi.status='pending' "
            "AND LOWER(COALESCE(t.track_type, 'music'))='jingle'",
            (self.station_id,),
        )
        jingle_ids = [int(row["id"]) for row in cur.fetchall()]
        if not jingle_ids:
            return 0
        placeholders = ",".join("?" for _ in jingle_ids)
        cur.execute(
            f"DELETE FROM queue_items WHERE id IN ({placeholders})",
            tuple(jingle_ids),
        )
        removed = int(cur.rowcount or 0)
        if removed > 0:
            self.conn.commit()
            _log.info("Sweeper disabled: removed %d pending jingles", removed)
        return removed

    def _next_pending_is_jingle(self) -> bool:
        """Check if the first pending item is already a jingle."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='pending' "
            "ORDER BY q.position ASC, q.id ASC LIMIT 1",
            (self.station_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        return str(row["track_type"] or "music").strip().lower() == "jingle"

    def _maybe_insert_sweeper_jingle(self) -> bool:
        """If sweeper is enabled and interval reached, insert a jingle
        at the front of the pending queue. Returns True if a jingle was inserted."""
        sweeper = self._get_sweeper_settings()
        if not sweeper["enabled"]:
            return False

        # Don't insert if a jingle is already at the front of the pending queue
        if self._next_pending_is_jingle():
            return False

        if sweeper["interval_unit"] == "minutes":
            elapsed_seconds = self._music_seconds_since_last_jingle(sweeper["baseline_queue_id"])
            if elapsed_seconds < float(sweeper["interval"]) * 60.0:
                return False
        else:
            music_count = self._count_music_since_last_jingle()
            if music_count < sweeper["interval"]:
                return False

        jingle = self._pick_random_jingle()
        if not jingle:
            _log.debug("Sweeper: no jingle tracks available")
            return False

        # Reserve two front positions. When an ad exists it must follow the
        # station jingle, before the next song; without ads only the jingle is
        # inserted. Shifting pending positions avoids duplicate-position order
        # ambiguity when the queue already starts at position 1.
        cur = self.conn.cursor()
        ad = self._pick_random_ad()
        reserve = 2 if ad else 1
        cur.execute(
            "UPDATE queue_items SET position=position+? "
            "WHERE station_id=? AND status='pending'",
            (reserve, self.station_id),
        )
        insert_pos = 1

        cur.execute(
            "INSERT OR IGNORE INTO queue_items (station_id, track_id, position, status, dedupe_key) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (self.station_id, jingle["track_id"], insert_pos,
             f"jingle:{jingle['track_id']}:{insert_pos}"),
        )
        if ad:
            cur.execute(
                "INSERT OR IGNORE INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                "VALUES (?, ?, 2, 'pending', ?)",
                (self.station_id, ad["track_id"], f"ad:{ad['track_id']}:2"),
            )
        self.conn.commit()
        if sweeper["interval_unit"] == "minutes":
            _log.info(
                "Sweeper: inserted station jingle track_id=%d after %.1f minutes at a song boundary",
                jingle["track_id"], elapsed_seconds / 60.0,
            )
        else:
            _log.info("Sweeper: inserted jingle (track_id=%d) after %d music tracks",
                      jingle["track_id"], music_count)
        return True

    # ------------------------------------------------------------------
    # Queue Autofill — keep ~30 minutes of tracks queued
    # ------------------------------------------------------------------
    _AUTOFILL_TARGET_SECONDS = 1800.0  # 30 minutes
    _AUTOFILL_MIN_MUSIC_TRACKS = 12

    def _pending_queue_duration(self) -> float:
        """Total duration of pending items in queue."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(t.duration), 0.0) AS total "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='pending'",
            (self.station_id,),
        )
        row = cur.fetchone()
        return float(row["total"] or 0.0) if row else 0.0

    def _pending_music_count(self) -> int:
        """Number of pending music tracks, excluding AI intros/jingles."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS count "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='pending' "
            "AND LOWER(COALESCE(t.track_type, 'music'))='music'",
            (self.station_id,),
        )
        row = cur.fetchone()
        return int(row["count"] or 0) if row else 0

    def _active_track_ids(self) -> set[int]:
        """Track IDs already pending/playing; hard-block these from autofill."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT DISTINCT track_id FROM queue_items "
            "WHERE station_id=? AND status IN ('pending','playing') "
            "AND COALESCE(track_id, 0) > 0",
            (self.station_id,),
        )
        return {int(r["track_id"]) for r in cur.fetchall()}

    def _fail_cross_station_queue_items(self) -> int:
        """Quarantine queue rows whose track belongs to another station."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.id "
            "FROM queue_items q "
            "JOIN tracks t ON t.id=q.track_id "
            "WHERE q.station_id=? AND t.station_id<>? "
            "AND q.status IN ('pending','playing')",
            (self.station_id, self.station_id),
        )
        invalid_ids = [int(row["id"]) for row in cur.fetchall()]
        if not invalid_ids:
            return 0

        placeholders = ",".join("?" for _ in invalid_ids)
        cur.execute(
            f"UPDATE queue_items SET status='failed', "
            f"finished_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            tuple(invalid_ids),
        )
        if int(cur.rowcount or 0) <= 0:
            return False
        self.conn.commit()

        current = self.playout_state.get_current(self.station_id)
        if (
            str(current.get("source") or "") == "manual"
            and current.get("item_id") is not None
            and int(current["item_id"]) in invalid_ids
        ):
            self._set_playout_state(
                "none", None, reason="manual_cross_station_track_quarantined"
            )

        _log.error(
            "Quarantined %d cross-station queue item(s) for station_id=%s",
            len(invalid_ids),
            self.station_id,
        )
        self._broadcast_worker_state(include_queue=True, include_track=True)
        return len(invalid_ids)

    def _recent_track_ids(self, limit: int = 30) -> set[int]:
        """Get recently played/queued track_ids to avoid repeats."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT DISTINCT track_id FROM ("
            "  SELECT track_id FROM queue_items "
            "  WHERE station_id=? AND COALESCE(track_id, 0) > 0 "
            "  ORDER BY id DESC LIMIT ?"
            ") recent_queue",
            (self.station_id, max(1, int(limit))),
        )
        return {int(r["track_id"]) for r in cur.fetchall()}

    def _select_random_music_track(self, exclude_ids: set[int]) -> dict | None:
        """Pick a least-played track from the active daypart without risking silence."""
        cur = self.conn.cursor()
        blocked = sorted({int(x) for x in exclude_ids if int(x) > 0})
        where = [
            "is_active=1",
            "COALESCE(file_path, '') <> ''",
            "LOWER(COALESCE(track_type, 'music'))='music'",
            "COALESCE(exclude_from_autoplay, 0)=0",
            "station_id=?",
        ]
        params: list = [self.station_id]
        if blocked:
            placeholders = ",".join("?" for _ in blocked)
            where.append(f"id NOT IN ({placeholders})")
            params.extend(blocked)
        select = (
            "SELECT id, COALESCE(duration, 0.0) AS duration, "
            "COALESCE(play_count, 0) AS play_count, COALESCE(bpm, 0.0) AS bpm "
            "FROM tracks WHERE "
        )

        def _candidates(extra_where: str = "", extra_params: tuple = ()) -> list:
            clauses = list(where)
            if extra_where:
                clauses.append(extra_where)
            query = (
                select
                + " AND ".join(clauses)
                + " ORDER BY COALESCE(play_count, 0) ASC, id ASC LIMIT 512"
            )
            cur.execute(query, tuple(params) + tuple(extra_params))
            return list(cur.fetchall())

        rule = active_daypart(self.conn, self.station_id)
        if rule is not None:
            try:
                policy = self.conn.execute(
                    "SELECT genre FROM rtai_daypart_policies WHERE station_id=? "
                    "AND day_of_week=? AND position=?",
                    (self.station_id, int(rule.day_of_week), int(rule.position)),
                ).fetchone()
            except Exception:
                policy = None
            genres = [
                item.strip().casefold()
                for item in str(policy["genre"] if policy else "").split(",")
                if item.strip()
            ]
            genre_clause = ""
            genre_params: tuple = ()
            if genres:
                genre_clause = " AND (" + " OR ".join(
                    "LOWER(COALESCE(genre, '')) LIKE ?" for _ in genres
                ) + ")"
                genre_params = tuple(f"%{item}%" for item in genres)
            rows = _candidates(
                "COALESCE(bpm, 0)>0 AND bpm>=? AND bpm<=?" + genre_clause,
                (float(rule.min_bpm), float(rule.max_bpm), *genre_params),
            )
            # Untagged libraries must keep playing while BPM analysis catches up.
            if not rows:
                rows = _candidates("COALESCE(bpm, 0)<=0" + genre_clause, genre_params)
            # A genre policy is preferred, but never allowed to cause silence.
            if not rows and genres:
                rows = _candidates(
                    "COALESCE(bpm, 0)>0 AND bpm>=? AND bpm<=?",
                    (float(rule.min_bpm), float(rule.max_bpm)),
                )
            # A narrow/custom range must never stall the transmitter.
            if not rows:
                rows = _candidates()
        else:
            rows = _candidates()
        if not rows:
            return None
        least_play_count = int(rows[0]["play_count"] or 0)
        tier = [
            row
            for row in rows
            if int(row["play_count"] or 0) == least_play_count
        ]
        station_settings = self._station_settings()
        seed = str(
            station_settings.get("autoplay_shuffle_seed")
            or f"radiotedu-onair-station-{self.station_id}"
        ).strip() or f"radiotedu-onair-station-{self.station_id}"
        row = min(
            tier,
            key=lambda candidate: hashlib.sha256(
                f"{seed}:{int(candidate['id'])}".encode("utf-8")
            ).digest(),
        )
        track_id = int(row["id"])
        duration = float(row["duration"] or 0.0)
        if duration <= 0.0:
            duration = self._probe_and_store_track_duration(track_id)
        return {"track_id": track_id, "duration": duration}

    def _autofill_queue(self) -> int:
        """Fill queue with random music tracks until enough duration and count.
        Interleaves jingles based on sweeper settings. Returns number added."""
        pending_duration = self._pending_queue_duration()
        pending_music_count = self._pending_music_count()
        if (
            pending_duration >= self._AUTOFILL_TARGET_SECONDS
            and pending_music_count >= self._AUTOFILL_MIN_MUSIC_TRACKS
        ):
            return 0

        sweeper = self._get_sweeper_settings()
        active_ids = self._active_track_ids()
        repetition_window = 30
        active_rule = active_daypart(self.conn, self.station_id)
        if active_rule is not None:
            try:
                policy = self.conn.execute(
                    "SELECT repetition_window FROM rtai_daypart_policies WHERE station_id=? "
                    "AND day_of_week=? AND position=?",
                    (self.station_id, int(active_rule.day_of_week), int(active_rule.position)),
                ).fetchone()
            except Exception:
                policy = None
            if policy is not None:
                repetition_window = max(5, min(200, int(policy["repetition_window"] or 30)))
        recent_ids = self._recent_track_ids(limit=repetition_window) | active_ids
        added = 0

        # Get next position
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM queue_items WHERE station_id=?",
            (self.station_id,),
        )
        next_pos = int(cur.fetchone()["next_pos"])

        # Count music since last jingle in pending items
        music_in_pending = self._count_pending_music_since_jingle()

        safety = 0
        while (
            pending_duration < self._AUTOFILL_TARGET_SECONDS
            or pending_music_count < self._AUTOFILL_MIN_MUSIC_TRACKS
        ):
            safety += 1
            if safety > 100:
                _log.warning("Autofill stopped at safety limit for station %d", self.station_id)
                break

            # Check if a jingle should be inserted
            if (
                sweeper["enabled"]
                and sweeper["interval_unit"] == "tracks"
                and music_in_pending >= sweeper["interval"]
            ):
                jingle = self._pick_random_jingle()
                if jingle:
                    cur.execute(
                        "INSERT OR IGNORE INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                        "VALUES (?, ?, ?, 'pending', ?)",
                        (self.station_id, jingle["track_id"], next_pos,
                         f"jingle:{jingle['track_id']}:{next_pos}"),
                    )
                    if int(cur.rowcount or 0) <= 0:
                        cur.execute(
                            "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos "
                            "FROM queue_items WHERE station_id=?",
                            (self.station_id,),
                        )
                        next_pos = int(cur.fetchone()["next_pos"])
                        music_in_pending = 0
                        continue
                    next_pos += 1
                    added += 1
                    music_in_pending = 0
                    # Jingle duration is short, add it
                    cur2 = self.conn.cursor()
                    cur2.execute("SELECT COALESCE(duration, 0) AS d FROM tracks WHERE id=?",
                                (jingle["track_id"],))
                    jr = cur2.fetchone()
                    if jr:
                        pending_duration += float(jr["d"] or 0.0)
                    ad = self._pick_random_ad()
                    if ad:
                        cur.execute(
                            "INSERT OR IGNORE INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                            "VALUES (?, ?, ?, 'pending', ?)",
                            (self.station_id, ad["track_id"], next_pos,
                             f"ad:{ad['track_id']}:{next_pos}"),
                        )
                        if int(cur.rowcount or 0) > 0:
                            next_pos += 1
                            added += 1
                            ad_row = self.conn.execute(
                                "SELECT COALESCE(duration, 0) AS d FROM tracks WHERE id=?",
                                (ad["track_id"],),
                            ).fetchone()
                            if ad_row:
                                pending_duration += float(ad_row["d"] or 0.0)

            # Add a music track
            track = self._select_random_music_track(recent_ids)
            if not track and recent_ids != active_ids:
                # Keeping audio continuous is more important than avoiding
                # older history forever; never duplicate active queue items.
                recent_ids = set(active_ids)
                track = self._select_random_music_track(recent_ids)
            if not track:
                break  # no music available
            recent_ids.add(track["track_id"])
            active_ids.add(track["track_id"])
            cur.execute(
                "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (self.station_id, track["track_id"], next_pos,
                 f"music:{track['track_id']}:{next_pos}"),
            )
            next_pos += 1
            added += 1
            music_in_pending += 1
            pending_music_count += 1
            pending_duration += track["duration"]

        if added > 0:
            self.conn.commit()
            _log.info(
                "Autofill: added %d items to queue (%.0f sec pending, %d music tracks)",
                added,
                pending_duration,
                pending_music_count,
            )
        return added

    def _prefetch_upcoming_audio(self, limit: int = 3) -> None:
        """Warm the next few H:/network tracks before a transition.

        The decoder must never perform a cold-volume copy on the handoff
        thread.  ``prefetch_fast_cached_uri`` is asynchronous and atomic, so
        this stays cheap even when the cache is already warm.
        """

        try:
            rows = self.conn.execute(
                "SELECT t.file_path FROM queue_items q "
                "JOIN tracks t ON t.id=q.track_id "
                "WHERE q.station_id=? AND q.status='pending' "
                "ORDER BY q.position, q.id LIMIT ?",
                (self.station_id, max(1, min(5, int(limit)))),
            ).fetchall()
        except Exception:
            return
        for row in rows:
            try:
                uri = resolve_runtime_media_path(str(row["file_path"] or ""))
                prefetch_fast_cached_uri(uri)
            except Exception:
                continue

    def _count_pending_music_since_jingle(self) -> int:
        """Count music tracks at the end of pending queue since last jingle."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='pending' "
            "ORDER BY q.position DESC, q.id DESC",
            (self.station_id,),
        )
        count = 0
        for row in cur.fetchall():
            tt = str(row["track_type"] or "music").strip().lower()
            if tt == "jingle":
                break
            if tt == "music":
                count += 1
        return count

    def _last_done_track_id(self) -> int:
        """Return the most recently completed music track_id, or 0."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.track_id FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='done' "
            "AND LOWER(COALESCE(t.track_type, 'music'))='music' "
            "ORDER BY COALESCE(q.finished_at, q.started_at, q.enqueued_at) DESC, "
            "q.id DESC LIMIT 1",
            (self.station_id,),
        )
        row = cur.fetchone()
        return int(row["track_id"]) if row else 0

    def _skip_duplicate_pending(self, pending) -> dict | None:
        """If the next pending track is the same as the just-finished one,
        mark it done and return the *new* next pending (or None).
        Skips ALL consecutive duplicates, not just the first one."""
        if not pending:
            return pending
        last_tid = self._last_done_track_id()
        if last_tid <= 0:
            return pending
        max_skips = 20  # safety limit
        skipped = 0
        while pending and skipped < max_skips:
            try:
                ptype = str(pending["track_type"] or "music").strip().lower()
            except (KeyError, IndexError):
                ptype = "music"
            if ptype != "music":
                break
            if int(pending["track_id"]) != last_tid:
                break
            _log.info(
                "Skipping duplicate pending track_id=%d (same as just finished)",
                last_tid,
            )
            self.queue_repo.mark_done(int(pending["id"]))
            skipped += 1
            pending = self.queue_repo.next_pending(self.station_id)
        return pending

    # ------------------------------------------------------------------
    # Show Lifecycle
    # ------------------------------------------------------------------
    def _get_active_show_session(self) -> dict | None:
        return ShowSessionRepository(self.conn).get_active_for_station(self.station_id)

    def _play_show_audio(self, file_path: str, source_name: str) -> bool:
        """Start playing a show audio file. Returns True if playback started."""
        resolved = resolve_runtime_media_path(str(file_path or ""))
        if not resolved:
            return False
        if not Path(resolved).is_file():
            return False
        try:
            if self.runtime_registry:
                self.runtime_registry.start_station(
                    self.station_id,
                    resolved,
                    stream_title=source_name.replace("_", " ").title(),
                    track_type="jingle",
                    crossfade_seconds=0.0,
                )
            self._set_playout_state(
                source_name, None, reason="show_audio_start"
            )
            self._broadcast_worker_state(include_track=True)
            return True
        except Exception:
            _log.exception("Failed to play show audio: %s", file_path)
            return False

    def _update_show_session_status(self, session_id: int, new_status: str) -> None:
        session = ShowSessionRepository(self.conn).get(session_id)
        old_status = session["status"] if session else "unknown"
        ShowSessionRepository(self.conn).update_status(session_id, new_status)
        try:
            from app.repositories.log_repo import LogRepository
            show_id = int(session["show_id"]) if session else 0
            LogRepository(self.conn).add_operation_log(
                station_id=self.station_id,
                message=f"Show {show_id}: {old_status} → {new_status}",
                event_type="show.transition",
                payload={"show_id": show_id, "from_status": old_status, "to_status": new_status},
            )
        except Exception:
            _log.warning("Failed to log show transition for session %s", session_id, exc_info=True)

    def _broadcast_show_event(self, event_type: str, session: dict, extra: dict | None = None) -> None:
        try:
            from app.ws.broadcaster import broadcaster
            payload = {
                "show_id": int(session["show_id"]),
                "session_id": int(session["id"]),
            }
            if extra:
                payload.update(extra)
            broadcaster.on_show_event(self.station_id, event_type, payload)
        except Exception:
            pass

    def _end_show_session(self, session: dict) -> None:
        from app.repositories.program_queue_repo import ProgramQueueRepository
        ShowSessionRepository(self.conn).end_session(int(session["id"]))
        ProgramQueueRepository(self.conn).set_source(self.station_id, "automation")
        self._set_playout_state("none", None, reason="show_session_end")
        self._broadcast_show_event("show.ended", session)
        self._broadcast_worker_state(include_queue=True, include_track=True)

    def _process_show_lifecycle(self, session: dict | None) -> dict | None:
        """Handle show lifecycle states. Returns a result dict if the state
        was fully handled (caller should return it), or None to fall through
        to normal automation."""
        if not session:
            return None

        status = session["status"]

        if status == "going_live":
            return self._handle_going_live(session)

        if status in ("intro_playing", "break_outro", "break_intro", "outro_playing"):
            return self._handle_show_audio_state(session)

        if status == "on_break":
            return self._handle_on_break(session)

        if status == "live":
            # Check queue low warning and ad break timing notifications
            self._check_show_queue_low(session)
            self._check_ad_break_timing(session)

        # "live" and "preparing" fall through to normal processing
        return None

    def _handle_going_live(self, session: dict) -> dict | None:
        """Wait for current track to finish, then play intro."""
        # Check if a queue item is still playing; advance happens in process_once
        playing = self.queue_repo.current_playing(self.station_id)
        if playing:
            return {"source": "show_hold", "reason": "waiting_for_track"}

        # Current track finished — play intro if available
        show = ShowRepository(self.conn).get(int(session["show_id"]))
        intro_path = show.get("intro_path") if show else None
        if intro_path:
            if self._play_show_audio(intro_path, "show_intro"):
                self._update_show_session_status(session["id"], "intro_playing")
                self._broadcast_show_event("show.intro_playing", session)
                return {"source": "show_intro", "reason": "intro_started"}

        # No intro — go directly to live
        self._update_show_session_status(session["id"], "live")
        self._broadcast_show_event("show.live", session, {"show_name": show["name"] if show else ""})
        return None  # Fall through to host queue

    def _handle_show_audio_state(self, session: dict) -> dict | None:
        """Check if show audio finished. If still playing, hold. If done, transition."""
        if self.runtime_registry:
            rt_status = self.runtime_registry.status(self.station_id)
            if self._runtime_playback_alive(rt_status):
                return {"source": f"show_{session['status']}", "reason": "audio_in_progress"}

        status = session["status"]
        show = ShowRepository(self.conn).get(int(session["show_id"]))

        if status == "intro_playing":
            self._update_show_session_status(session["id"], "live")
            self._broadcast_show_event("show.live", session, {"show_name": show["name"] if show else ""})
            return None  # Fall through to host queue

        if status == "break_outro":
            self._update_show_session_status(session["id"], "on_break")
            return None  # Fall through to let ad pipeline handle

        if status == "break_intro":
            self._update_show_session_status(session["id"], "live")
            self._broadcast_show_event("show.break_end", session)
            return None  # Fall through to host queue

        if status == "outro_playing":
            self._end_show_session(session)
            return {"source": "show_ended", "reason": "outro_finished"}

        return None

    def _handle_on_break(self, session: dict) -> dict | None:
        """During ad break: if ads remain, fall through to ad pipeline.
        If no more ads, play break intro and return to live."""
        due_ad = self._next_due_ad_if_allowed()
        if due_ad:
            return None  # Fall through — ad pipeline will handle

        # No more ads — play break intro if available
        show = ShowRepository(self.conn).get(int(session["show_id"]))
        break_intro_path = show.get("break_intro_path") if show else None
        if break_intro_path:
            if self._play_show_audio(break_intro_path, "show_break_intro"):
                self._update_show_session_status(session["id"], "break_intro")
                return {"source": "show_break_intro", "reason": "break_intro_started"}

        # No break intro — go directly to live
        self._update_show_session_status(session["id"], "live")
        self._broadcast_show_event("show.break_end", session)
        return None

    def _check_show_queue_low(self, session: dict) -> None:
        """During a live show, broadcast show.queue_low when host queue drops below minimum.
        Debounced to at most once per minute to avoid broadcast storms."""
        import time
        try:
            settings = SettingsRepository(self.conn).get_station(self.station_id)
            min_tracks = int(settings.get("show_min_queue_tracks", "3") or "3")
            cur = self.conn.cursor()
            cur.execute(
                "SELECT COUNT(*) as cnt FROM queue_items WHERE station_id = ? AND status = 'pending'",
                (self.station_id,),
            )
            remaining = int(cur.fetchone()["cnt"])
            debounce_key = ("queue_low", self.station_id, int(session["id"]))
            if remaining >= min_tracks:
                _show_notification_sent.pop(debounce_key, None)
                return
            now = time.monotonic()
            if now - _show_notification_sent.get(debounce_key, 0.0) < _SHOW_QUEUE_LOW_DEBOUNCE_SEC:
                return
            _show_notification_sent[debounce_key] = now
            self._broadcast_show_event(
                "show.queue_low", session,
                {"remaining": remaining, "min_tracks": min_tracks},
            )
        except Exception:
            pass

    def _check_ad_break_timing(self, session: dict) -> None:
        """Broadcast ad_break.upcoming notification when a break is within advance notice window.
        Also mark overdue ad breaks as 'missed' and broadcast ad_break.missed."""
        import datetime
        try:
            settings = SettingsRepository(self.conn).get_station(self.station_id)
            advance_minutes = int(settings.get("ad_break_advance_notice", "5") or "5")
            tolerance_minutes = int(settings.get("ad_break_tolerance_minutes", "10") or "10")
            now = datetime.datetime.now(datetime.timezone.utc)
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM ad_break_items WHERE station_id = ? AND status = 'pending' "
                "ORDER BY due_at ASC LIMIT 1",
                (self.station_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            due_at_str = str(row["due_at"] or "")
            try:
                due_at = datetime.datetime.fromisoformat(due_at_str.replace(" ", "T"))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=datetime.timezone.utc)
            except (ValueError, AttributeError):
                return

            tolerance_until = due_at + datetime.timedelta(minutes=tolerance_minutes)

            # Mark as missed if tolerance window exceeded
            if now > tolerance_until:
                cur.execute(
                    "UPDATE ad_break_items SET status='missed' WHERE id = ?",
                    (int(row["id"]),),
                )
                self.conn.commit()
                _show_notification_sent.pop(("ad_upcoming", self.station_id, int(row["id"])), None)
                self._broadcast_show_event(
                    "ad_break.missed", session,
                    {"item_id": int(row["id"]), "due_at": due_at_str},
                )
                return

            # Broadcast upcoming warning within advance notice window (debounced)
            notice_window = due_at - datetime.timedelta(minutes=advance_minutes)
            if now >= notice_window:
                debounce_key = ("ad_upcoming", self.station_id, int(row["id"]))
                last_sent = _show_notification_sent.get(debounce_key, 0.0)
                if now.timestamp() - last_sent >= _AD_BREAK_UPCOMING_DEBOUNCE_SEC:
                    _show_notification_sent[debounce_key] = now.timestamp()
                    self._broadcast_show_event(
                        "ad_break.upcoming", session,
                        {
                            "item_id": int(row["id"]),
                            "due_at": due_at_str,
                            "tolerance_until": tolerance_until.strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
        except Exception:
            pass

    def process_once(self) -> dict:
        if not self.lease_service.try_acquire(self.station_id, self.worker_id):
            return {"source": "none", "reason": "lease_denied"}

        # A media producer can remain alive after an Icecast/local sink exits.
        # Repair the branch in place; the registry preserves the current media
        # offset and enforces bounded retry backoff.
        health_check = getattr(
            self.runtime_registry,
            "required_outputs_healthy",
            None,
        )
        recover = getattr(self.runtime_registry, "recover_station", None)
        if (
            self.runtime_registry
            and callable(health_check)
            and callable(recover)
            and self.runtime_registry.is_process_running(self.station_id)
            and not health_check(self.station_id)
        ):
            recover(self.station_id)

        # ── Show lifecycle check ──────────────────────────────
        show_session = self._get_active_show_session()
        show_result = self._process_show_lifecycle(show_session)
        if show_result is not None:
            return show_result

        # On first tick after startup, insert the startup sound at front of queue
        self._maybe_insert_startup_sound()

        # A station is an isolation boundary. Repair legacy/corrupt queue rows
        # before measuring, filling, or selecting the next item.
        self._fail_cross_station_queue_items()

        # Auto-fill queue BEFORE advance check so that crossfade timing
        # can see the next pending track when deciding when to advance.
        self._autofill_queue()

        # Warm slow-volume media while the current song is still on air. This
        # removes the 35–40 second cold H: drive copy that used to occur
        # exactly at the song handoff.
        self._prefetch_upcoming_audio()

        # If sweeper is disabled, remove any lingering pending jingles.
        # If enabled, check if a sweeper jingle should be inserted at front.
        sweeper = self._get_sweeper_settings()
        if not sweeper["enabled"]:
            self._remove_pending_jingles()
        else:
            self._maybe_insert_sweeper_jingle()

        # Prepare queue-native AI announcements before the next song when due.
        self._maybe_prepare_ai_queue()

        # Background prefetch owns upcoming intro generation. Doing that work
        # inline here blocks the worker loop and stalls playback on cold cache.

        # Advance the currently-playing queue item if its duration has elapsed
        self._advance_playing_queue_item()

        # Ad rows are policy-gated and remain playing until their audio ends.
        self._advance_playing_ad_item()
        self._fail_disabled_active_ads()
        self._ensure_hourly_ad_break()

        # Advance host track if it finished playing
        self._advance_host_track()

        # If a host track is still playing, wait for it
        current_playout = self.playout_state.get_current(self.station_id)
        if current_playout["source"] == "host":
            return {"source": "playing", "reason": "host_track_in_progress"}

        playing = self.queue_repo.current_playing(self.station_id)
        pending = self.queue_repo.next_pending(self.station_id)

        # Prevent back-to-back same song (skip if duplicate of just-finished)
        if not playing:
            pending = self._skip_duplicate_pending(pending)

        due_ad = self._next_due_ad_if_allowed()
        ready_schedule = self.schedule_repo.next_ready(self.station_id)

        # Check host queue: if source is "host" and items exist, prefer them
        host_pending = None
        queue_source = self.program_queue_repo.get_source(self.station_id)
        if queue_source == "host":
            host_pending = self.program_queue_repo.next_pending(self.station_id)

        # While a queue item is playing, suppress manual count so ads/schedule
        # can still fire; the next queue item waits until the current finishes.
        manual_count = 1 if pending and not playing else 0
        # Suppress ad auto-fire during all active show states EXCEPT 'preparing' and 'on_break'.
        # 'preparing': normal automation, DJ hasn't gone live yet.
        # 'on_break': DJ explicitly initiated break, ad pipeline handles the ads.
        # All other active states (going_live, intro_playing, live, break_outro,
        # break_intro, outro_playing): ads must NOT auto-fire (spec Section 6.3).
        ad_suppressed = (
            show_session is not None
            and show_session["status"] not in ("preparing", "on_break")
        )
        # During 'on_break', suppress the normal queue so the ad pipeline takes priority.
        # Regular music should not play during a show ad break.
        if show_session is not None and show_session["status"] == "on_break":
            manual_count = 0
        source = choose_source(
            manual_count=manual_count,
            ad_due=False if ad_suppressed else bool(due_ad),
            schedule_ready=bool(ready_schedule),
            fallback_ready=bool(self.fallback_uri) and not bool(playing),
            host_count=1 if host_pending else 0,
        )
        if source == "host" and host_pending:
            host_item_id = int(host_pending["id"])
            track_id = int(host_pending["track_id"])
            return self._play_host_track(host_item_id, track_id)
        if source == "manual" and pending:
            item_id = int(pending["id"])
            track_id = int(pending["track_id"])
            return self._play_managed_item(
                source=source,
                item_id=item_id,
                track_id=track_id,
                mark_playing=self.queue_repo.mark_playing,
                mark_done=self.queue_repo.mark_done,
                mark_failed=self.queue_repo.mark_failed,
                auto_done=False,  # keep as 'playing' until duration expires
            )
        if source == "ads" and due_ad:
            self._finish_playing_queue_item()  # preempt queue track for ad
            item_id = int(due_ad["id"])
            track_id = int(due_ad["track_id"])
            return self._play_managed_item(
                source=source,
                item_id=item_id,
                track_id=track_id,
                mark_playing=self.ad_repo.mark_playing,
                mark_done=self.ad_repo.mark_done,
                mark_failed=self.ad_repo.mark_failed,
                auto_done=False,
            )
        if source == "schedule" and ready_schedule:
            self._finish_playing_queue_item()  # preempt queue track for schedule
            item_id = int(ready_schedule["id"])
            track_id = int(ready_schedule["track_id"])
            return self._play_managed_item(
                source=source,
                item_id=item_id,
                track_id=track_id,
                mark_playing=self.schedule_repo.mark_playing,
                mark_done=self.schedule_repo.mark_done,
                mark_failed=self.schedule_repo.mark_failed,
            )
        if source == "fallback" and self.runtime_registry and self.fallback_uri:
            fallback_title = (
                f"{self._station_name()} Continuity"
                if is_silence_input_uri(self.fallback_uri)
                else (
                    self._fallback_title_from_uri(self.fallback_uri)
                    or f"{self._station_name()} Continuity"
                )
            )
            self.runtime_registry.start_station(
                self.station_id,
                self.fallback_uri,
                stream_title=fallback_title,
                stream_artist="",
                track_type="announcement",
                crossfade_seconds=0.0,
            )
            return {
                "source": source,
                "input_uri": self.fallback_uri,
                "stream_title": fallback_title,
            }
        if playing:
            return {"source": "playing", "reason": "track_in_progress"}
        return {"source": source}
