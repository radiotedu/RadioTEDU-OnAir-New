from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.db import get_connection
from app.file_security import audio_upload_extensions

logger = logging.getLogger("cleanroom.managed_library")

_FOLDER_KEYS = {
    "music_library_folder": "music",
    "jingle_library_folder": "jingle",
    "ad_library_folder": "ad",
    "ads_library_folder": "ad",
    "station_id_library_folder": "station_id",
    "show_library_folder": "show",
}
_DEFAULT_RESCAN_INTERVAL_SECONDS = 600.0
_MAX_RETRY_DELAY_SECONDS = 300.0


@dataclass(frozen=True)
class ManagedLibraryProfile:
    station_id: int
    track_type: str
    folder: str
    recursive: bool = True
    mode: str = "merge"
    profile_label: str = ""
    default_genre: str = ""
    default_language: str = ""
    skip_unplayable: bool = True
    rescan_interval_seconds: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.station_id}:{self.track_type}"


@dataclass
class _ProfileState:
    fingerprint: str = ""
    stable_polls: int = 0
    synced_fingerprint: str = ""
    status: str = "watching"
    last_scan_at: float = 0.0
    last_sync_at: float = 0.0
    retry_count: int = 0
    next_retry_at: float = 0.0
    error: str = ""
    result: dict = field(default_factory=dict)


def _truthy(raw: str, default: bool = False) -> bool:
    token = str(raw or "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _nonnegative_float(raw: str, default: float = 0.0) -> float:
    try:
        return max(0.0, float(str(raw or "").strip()))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def _default_profile_provider() -> list[ManagedLibraryProfile]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT station_id, key, value FROM station_settings "
            "ORDER BY station_id ASC, key ASC"
        ).fetchall()
    finally:
        conn.close()

    settings_by_station: dict[int, dict[str, str]] = {}
    for row in rows:
        sid = int(row["station_id"])
        settings_by_station.setdefault(sid, {})[str(row["key"])] = str(
            row["value"] or ""
        )

    profiles: list[ManagedLibraryProfile] = []
    for station_id, settings in settings_by_station.items():
        for folder_key, track_type in _FOLDER_KEYS.items():
            folder = str(settings.get(folder_key, "") or "").strip()
            if not folder:
                continue
            prefix = "library" if track_type == "music" else f"{track_type}_library"
            profiles.append(
                ManagedLibraryProfile(
                    station_id=station_id,
                    track_type=track_type,
                    folder=folder,
                    recursive=_truthy(
                        settings.get(f"{prefix}_recursive", "true"), default=True
                    ),
                    mode=str(
                        settings.get(f"{prefix}_management_mode", "merge") or "merge"
                    ).strip(),
                    profile_label=str(
                        settings.get(f"{prefix}_profile_label", "") or ""
                    ).strip(),
                    default_genre=str(
                        settings.get(f"{prefix}_default_genre", "") or ""
                    ).strip(),
                    default_language=str(
                        settings.get(f"{prefix}_default_language", "") or ""
                    ).strip(),
                    skip_unplayable=_truthy(
                        settings.get(f"{prefix}_skip_unplayable", "true"),
                        default=True,
                    ),
                    rescan_interval_seconds=_nonnegative_float(
                        settings.get(
                            f"{prefix}_rescan_interval_seconds",
                            str(_DEFAULT_RESCAN_INTERVAL_SECONDS),
                        ),
                        default=_DEFAULT_RESCAN_INTERVAL_SECONDS,
                    ),
                )
            )
    return profiles


def _default_sync_callback(profile: ManagedLibraryProfile) -> dict:
    # Import lazily to avoid an API/service import cycle during application boot.
    from app.api.legacy import LibraryFolderSyncPayload, sync_station_library_folder
    from app.db import get_connection
    from app.engine.broadcast_queue_autofill import reconcile_pending_sweeper_queue

    result = sync_station_library_folder(
        LibraryFolderSyncPayload(
            station_id=profile.station_id,
            folder=profile.folder,
            recursive=profile.recursive,
            track_type=profile.track_type,
            mode=profile.mode,
            profile_label=profile.profile_label,
            default_genre=profile.default_genre,
            default_language=profile.default_language,
            skip_unplayable=profile.skip_unplayable,
            incremental=True,
            guard_configured_folder=True,
            allow_empty=True,
        )
    )
    # Replace-managed folders are authoritative. A deletion must disappear
    # from future playout immediately, and changed jingle/ad inventories must
    # be re-spaced against the current three-song cadence.
    conn = get_connection()
    try:
        reconcile_pending_sweeper_queue(conn, int(profile.station_id))
        conn.commit()
    finally:
        conn.close()
    return {**result, "queue_reconciled": True}


class ManagedLibraryWatcher:
    """Deterministically poll managed media folders and sync stable changes.

    The watcher deliberately requires two identical observations before an
    import. This prevents a file that is still being copied into a managed
    folder from being validated halfway through the write. Failures use bounded
    exponential retries and remain visible through ``snapshot``.
    """

    def __init__(
        self,
        *,
        profile_provider: Callable[[], list[ManagedLibraryProfile]] | None = None,
        sync_callback: Callable[[ManagedLibraryProfile], dict] | None = None,
        poll_interval_seconds: float = 5.0,
        required_stable_polls: int = 2,
        max_retries: int | None = None,
    ):
        self._profile_provider = profile_provider or _default_profile_provider
        self._sync_callback = sync_callback or _default_sync_callback
        self._poll_interval = max(0.2, float(poll_interval_seconds))
        self._required_stable_polls = max(1, int(required_stable_polls))
        self._max_retries = (
            None if max_retries is None else max(0, int(max_retries))
        )
        self._states: dict[str, _ProfileState] = {}
        self._profiles: dict[str, ManagedLibraryProfile] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _fingerprint(profile: ManagedLibraryProfile) -> tuple[str, int]:
        root = Path(profile.folder).expanduser()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Managed folder is unavailable: {root}")
        # Sidecar artwork participates in the fingerprint so replacing
        # cover/folder/front images refreshes metadata without touching audio.
        extensions = audio_upload_extensions() | {".jpg", ".jpeg", ".png", ".webp"}
        iterator = root.rglob("*") if profile.recursive else root.iterdir()
        records: list[tuple[str, int, int]] = []
        for path in iterator:
            try:
                if not path.is_file() or path.suffix.lower() not in extensions:
                    continue
                stat = path.stat()
                relative = path.relative_to(root).as_posix().casefold()
                records.append((relative, int(stat.st_size), int(stat.st_mtime_ns)))
            except (OSError, RuntimeError, ValueError):
                continue
        records.sort()
        digest = hashlib.sha256()
        for relative, size, modified_ns in records:
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(modified_ns).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest(), len(records)

    def poll_once(self, *, now: float | None = None) -> None:
        observed_at = float(time.time() if now is None else now)
        profiles = sorted(self._profile_provider(), key=lambda item: item.key)
        active_keys = {profile.key for profile in profiles}
        with self._lock:
            self._profiles = {profile.key: profile for profile in profiles}
            for stale_key in set(self._states) - active_keys:
                self._states.pop(stale_key, None)

        for profile in profiles:
            with self._lock:
                state = self._states.setdefault(profile.key, _ProfileState())
            try:
                fingerprint, file_count = self._fingerprint(profile)
            except Exception as exc:
                with self._lock:
                    state.status = "error"
                    state.last_scan_at = observed_at
                    state.error = str(exc)[:500]
                    state.result = {"file_count": 0}
                continue

            with self._lock:
                state.last_scan_at = observed_at
                state.result["file_count"] = file_count
                if fingerprint != state.fingerprint:
                    state.fingerprint = fingerprint
                    state.stable_polls = 1
                    state.retry_count = 0
                    state.next_retry_at = 0.0
                    state.error = ""
                    state.status = "settling"
                    continue
                state.stable_polls += 1
                periodic_rescan_due = (
                    profile.rescan_interval_seconds > 0.0
                    and state.last_sync_at > 0.0
                    and observed_at - state.last_sync_at
                    >= profile.rescan_interval_seconds
                )
                if fingerprint == state.synced_fingerprint and not periodic_rescan_due:
                    state.status = "watching"
                    continue
                if state.stable_polls < self._required_stable_polls:
                    state.status = "settling"
                    continue
                if (
                    self._max_retries is not None
                    and state.retry_count > self._max_retries
                ):
                    state.status = "failed"
                    continue
                if observed_at < state.next_retry_at:
                    state.status = "retry_wait"
                    continue
                state.status = "syncing"

            try:
                result = dict(self._sync_callback(profile) or {})
            except Exception as exc:
                with self._lock:
                    state.retry_count += 1
                    state.next_retry_at = observed_at + min(
                        _MAX_RETRY_DELAY_SECONDS,
                        float(2 ** min(20, max(0, state.retry_count - 1))),
                    )
                    state.status = (
                        "failed"
                        if self._max_retries is not None
                        and state.retry_count > self._max_retries
                        else "retry_wait"
                    )
                    state.error = str(exc)[:500]
                logger.exception(
                    "Managed-folder sync failed station=%s type=%s attempt=%s",
                    profile.station_id,
                    profile.track_type,
                    state.retry_count,
                )
                continue

            with self._lock:
                state.synced_fingerprint = fingerprint
                state.last_sync_at = observed_at
                state.retry_count = 0
                state.next_retry_at = 0.0
                state.status = "watching"
                state.error = ""
                state.result = {**result, "file_count": file_count}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="managed-library-watcher",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Managed-folder watcher poll failed")
            self._wake_event.wait(self._poll_interval)
            self._wake_event.clear()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    def request_rescan(
        self, *, station_id: int | None = None, track_type: str | None = None
    ) -> int:
        selected = 0
        normalized_type = str(track_type or "").strip().lower()
        with self._lock:
            for key, profile in self._profiles.items():
                if station_id is not None and profile.station_id != int(station_id):
                    continue
                if normalized_type and profile.track_type != normalized_type:
                    continue
                state = self._states.setdefault(key, _ProfileState())
                state.synced_fingerprint = ""
                state.stable_polls = self._required_stable_polls
                state.retry_count = 0
                state.next_retry_at = 0.0
                state.status = "queued"
                selected += 1
        self._wake_event.set()
        return selected

    def snapshot(self) -> dict:
        with self._lock:
            profiles = []
            for key in sorted(self._profiles):
                profile = self._profiles[key]
                state = self._states.get(key, _ProfileState())
                profiles.append(
                    {
                        "station_id": profile.station_id,
                        "track_type": profile.track_type,
                        "folder": profile.folder,
                        "recursive": profile.recursive,
                        "mode": profile.mode,
                        "status": state.status,
                        "last_scan_at": state.last_scan_at,
                        "last_sync_at": state.last_sync_at,
                        "retry_count": state.retry_count,
                        "retry_forever": self._max_retries is None,
                        "error": state.error,
                        "result": dict(state.result),
                    }
                )
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "profiles": profiles,
            }


_managed_library_watcher = ManagedLibraryWatcher()


def get_managed_library_watcher() -> ManagedLibraryWatcher:
    return _managed_library_watcher
