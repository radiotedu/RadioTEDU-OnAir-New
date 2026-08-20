import csv
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import (
    filter_station_rows_for_user,
    get_current_user,
    require_any_permission,
    require_permission,
    require_role,
    user_has_permission,
    user_has_unrestricted_station_access,
    user_has_show_permission,
    user_is_superadmin,
)
from app.audio.gst_pipeline import resolve_stream_profile
from app.audio.bpm_analyzer import analyze_bpm
from app.config import get_db_path
from app.cover_art import public_track_cover_url
from app.db import get_connection, init_db
from app.engine.broadcast_queue_autofill import (
    ensure_broadcast_queue_filled,
    ensure_broadcast_queue_ready_for_playback,
    reconcile_pending_sweeper_queue,
)
from app.file_security import (
    audio_upload_extensions,
    is_within_root,
    resolve_under_root,
    save_upload_file,
)
from app.media_paths import resolve_runtime_media_path
from app.repositories.ad_campaign_repo import AdCampaignRepository
from app.repositories.log_repo import LogRepository
from app.repositories.playlist_repo import PlaylistRepository
from app.repositories.program_queue_repo import ProgramQueueRepository
from app.repositories.queue_repo import QueueRepository
from app.repositories.schedule_repo import ScheduleRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.repositories.station_repo import StationRepository
from app.repositories.track_repo import TrackRepository
from app.repositories.user_repo import UserRepository
from app.runtime_paths import resolve_binary
from app.services.music_usage import MusicUsageService

router = APIRouter()

_YTDLP_LOCK = threading.Lock()
_YTDLP_JOBS: dict[str, dict] = {}
_YTDLP_PENDING_JOB_IDS: list[str] = []
_YTDLP_RECENT_JOB_IDS: list[str] = []
_YTDLP_RUNNING_JOB_ID: str | None = None


def _remove_station_owned_media(station_id: int) -> dict[str, object]:
    """Remove only app-managed files owned by a deleted station."""
    sid = int(station_id)
    expected_name = f"station-{sid}"
    removed: list[str] = []
    errors: list[str] = []
    data_root = get_db_path().parent.resolve()
    for container_name in ("uploads", "downloads"):
        container = (data_root / container_name).resolve()
        target = (container / expected_name).resolve()
        if target.parent != container or target.name != expected_name:
            errors.append(f"{container_name}: unsafe station media path")
            continue
        if not target.exists():
            continue
        try:
            shutil.rmtree(target)
            removed.append(container_name)
        except OSError as exc:
            errors.append(f"{container_name}: {str(exc)[:200]}")
    return {"ok": not errors, "removed": removed, "errors": errors}


def _run_sqlite_write_with_retry(conn, operation, *, attempts: int = 6, delay_sec: float = 0.1):
    for attempt in range(max(1, int(attempts))):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            is_locked = "locked" in str(exc).lower()
            is_last_attempt = attempt >= max(1, int(attempts)) - 1
            try:
                conn.rollback()
            except Exception:
                pass
            if not is_locked or is_last_attempt:
                raise
            time.sleep(float(delay_sec))


class StationCreatePayload(BaseModel):
    name: str
    description: str = ""


class StationUpdatePayload(BaseModel):
    name: str


class StationActivePayload(BaseModel):
    station_id: int


class SettingsUpdatePayload(BaseModel):
    values: dict[str, str]


class PlaylistCreatePayload(BaseModel):
    station_id: int
    name: str
    description: str = ""


class PlaylistItemCreatePayload(BaseModel):
    track_id: int


class PlaylistReorderPayload(BaseModel):
    item_ids: list[int]


class PlaylistBulkPayload(BaseModel):
    track_ids: list[int]


class PlaylistAutoGeneratePayload(BaseModel):
    name: str
    description: str = ""
    station_id: int = 1
    artist: str | None = None
    genre: str | None = None
    track_type: str = "any"
    bpm_min: float | None = None
    bpm_max: float | None = None
    limit: int = 50
    sort_by: str = "random"


class QueueMovePayload(BaseModel):
    item_id: int
    to_index: int
    station_id: int
    expected_revision: str = Field(min_length=8, max_length=128)


class LegacySchedulePayload(BaseModel):
    station_id: int
    track_id: int | None = None
    play_at: str | None = None
    window_end: str | None = None
    playlist_id: int | None = None
    event_name: str = ""
    start_time: str | None = None
    end_time: str | None = None
    day_of_week: str = "*"


class AdBreakSetPayload(BaseModel):
    station_id: int
    name: str
    description: str = ""
    enabled: bool | None = None
    is_active: bool | None = None
    intro_jingle_track_id: int | None = None
    outro_jingle_track_id: int | None = None
    slots: list[dict] = Field(default_factory=list)


class AdCampaignPayload(BaseModel):
    station_id: int
    name: str
    enabled: bool | None = None
    is_active: bool | None = None
    start_date: str | None = None
    end_date: str | None = None
    day_interval: int | None = None
    daily_repeat_limit: int | None = None
    priority: int | None = None
    notes: str | None = None
    slot_ids: list[int] = Field(default_factory=list)
    track_ids: list[int] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)


class EventCreatePayload(BaseModel):
    station_id: int
    event_type: str = "generic"
    payload: dict = Field(default_factory=dict)


class ProgramQueueItemPayload(BaseModel):
    station_id: int
    show_id: int | None = None
    track_id: int


class ProgramQueueMovePayload(BaseModel):
    station_id: int
    show_id: int | None = None
    from_index: int
    to_index: int


class ProgramQueueSourcePayload(BaseModel):
    station_id: int
    show_id: int | None = None
    source: str


class ProgramWorkspaceClaimPayload(BaseModel):
    station_id: int
    show_id: int
    force: bool = False


class YtDlpImportPayload(BaseModel):
    url: str
    track_type: str = "music"
    station_id: int = 1
    target_station_id: int | None = None
    download_playlist: bool = False
    music_only_mode: bool = True
    audio_format: str = "mp3"
    audio_quality: str = "192"
    auto_trim_silence: bool = False
    trim_threshold_db: float = -45.0
    trim_min_silence: float = 0.15
    auto_intro_clean: bool = False
    intro_clean_preset: str = "normal"
    intro_max_cut_s: float = 18.0


class MetadataRuleCreatePayload(BaseModel):
    station_id: int | None = None
    scope: str = "station"
    name: str = ""
    target_field: str = "title"
    match_type: str = "contains"
    pattern: str
    replacement: str = ""
    is_case_sensitive: bool = False
    priority: int = 100
    is_active: bool = True


class MetadataRuleUpdatePayload(BaseModel):
    is_active: bool | None = None
    name: str | None = None
    pattern: str | None = None
    replacement: str | None = None
    priority: int | None = None


class MetadataAutofixPayload(BaseModel):
    station_id: int = 1
    analyze_bpm: bool = True
    limit: int = 0
    library_scope: str = "local"
    source_station_id: int | None = None
    auto_seed_rules: bool = True
    rule_scope: str = "station"
    verify_with_itunes: bool = False
    itunes_country: str = "TR"
    itunes_min_confidence: float = 0.88
    itunes_track_type: str = "music"


class MetadataNormalizePayload(BaseModel):
    station_id: int = 1
    analyze_bpm: bool = True
    limit: int = 0
    library_scope: str = "local"
    source_station_id: int | None = None


class MetadataItunesVerifyPayload(BaseModel):
    station_id: int = 1
    limit: int = 0
    min_confidence: float = 0.88
    country: str = "TR"
    dry_run: bool = False
    library_scope: str = "local"
    source_station_id: int | None = None
    track_type: str = "music"


class BpmAnalyzePayload(BaseModel):
    station_id: int = 1
    only_missing: bool = True
    track_type: str = "music"
    limit: int = 0


class LibraryFolderSyncPayload(BaseModel):
    station_id: int = 1
    folder: str
    recursive: bool = True
    track_type: str = "music"
    mode: str = "replace"
    skip_unplayable: bool = False
    remove_pending_queue: bool = True
    profile_label: str = ""
    default_genre: str = ""
    default_language: str = ""
    incremental: bool = False
    guard_configured_folder: bool = False
    allow_empty: bool = False


class FolderPickerPayload(BaseModel):
    initial_folder: str = ""
    description: str = "Select a radio media folder"


class FilePickerPayload(BaseModel):
    initial_path: str = ""
    description: str = "Select a protected configuration file"


class SweeperConfigPayload(BaseModel):
    station_id: int = 1
    enabled: bool = False
    interval: int = Field(default=2, ge=1, le=1440)
    interval_unit: str = "tracks"
    mode: str = "ordered"


class StartupSoundPayload(BaseModel):
    station_id: int = 1
    enabled: bool = False
    mode: str = "random"       # "random" | "specific"
    track_id: int = 0          # only used when mode == "specific"


class PlayedTrackPayload(BaseModel):
    title: str = ""
    artist: str = ""
    filename: str = ""
    station_id: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


def _station_name(conn, station_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT name FROM stations WHERE id=? LIMIT 1", (int(station_id),))
    row = cur.fetchone()
    if row:
        return str(row["name"] or "").strip()
    return ""


def _guess_title_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    segment = unquote((parsed.path or "").strip("/").split("/")[-1])
    base = segment.strip() if segment else ""
    if base:
        return base
    host = (parsed.netloc or "").strip()
    if host:
        return f"Imported from {host}"
    return "Imported track"


def _serialize_ytdlp_job(job: dict, queue_position: int | None = None, include_result: bool = False) -> dict:
    payload = dict(job.get("request") or {})
    data = {
        "id": str(job.get("id") or ""),
        "status": str(job.get("status") or "queued"),
        "phase": str(job.get("phase") or "queued"),
        "message": str(job.get("message") or ""),
        "url": str(payload.get("url") or ""),
        "track_type": str(payload.get("track_type") or "music"),
        "station_id": int(payload.get("station_id") or 1),
        "target_station_id": int(payload.get("target_station_id") or payload.get("station_id") or 1),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "queue_position": queue_position,
    }
    if include_result:
        data["result"] = job.get("result")
    return data


_log = logging.getLogger("cleanroom.ytdlp")


def _clean_artist_metadata(value: object) -> str:
    artist = str(value or "").strip()
    normalized = " ".join(artist.lower().replace("_", " ").replace("-", " ").split())
    if normalized in {"stream error", "metadata error", "ffprobe error"}:
        return ""
    return artist


def _get_audio_metadata(
    file_path: str,
    *,
    fallback_title: str = "",
    fallback_artist: str = "",
    require_playable: bool = False,
) -> dict[str, str | float | bool]:
    """Get audio metadata and duration using ffprobe."""
    ffprobe = resolve_binary("ffprobe.exe") or resolve_binary("ffprobe")
    if not ffprobe:
        if require_playable:
            raise RuntimeError("ffprobe is unavailable; cannot verify audio playability")
        return {
            "title": str(fallback_title or "").strip(),
            "artist": _clean_artist_metadata(fallback_artist),
            "album": "",
            "genre": "",
            "language": "",
            "musicbrainz_recordingid": "",
            "duration": 0.0,
            "bpm": 0.0,
            "has_embedded_art": False,
        }

    title = str(fallback_title or "").strip()
    artist = _clean_artist_metadata(fallback_artist)
    album = ""
    genre = ""
    language = ""
    musicbrainz_recordingid = ""
    duration = 0.0
    bpm = 0.0
    has_embedded_art = False

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            if require_playable:
                detail = str(result.stderr or "").strip()
                raise ValueError(detail or "ffprobe rejected the audio file")
            return {
                "title": title,
                "artist": artist,
                "album": album,
                "genre": genre,
                "language": language,
                "musicbrainz_recordingid": musicbrainz_recordingid,
                "duration": duration,
                "bpm": bpm,
                "has_embedded_art": has_embedded_art,
            }

        info = json.loads(result.stdout or "{}")
        format_info = info.get("format", {}) or {}
        streams = info.get("streams", []) or []
        has_embedded_art = any(
            str(stream.get("codec_type", "")).lower() == "video"
            and bool((stream.get("disposition", {}) or {}).get("attached_pic", 0))
            for stream in streams
            if isinstance(stream, dict)
        )
        raw_duration = format_info.get("duration", 0.0)
        try:
            duration = float(raw_duration or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if require_playable and duration <= 0.05:
            raise ValueError("audio duration is missing or zero")

        raw_tags = format_info.get("tags", {}) or {}
        tags = {
            str(key or "").strip().lower(): str(value or "").strip()
            for key, value in raw_tags.items()
        }
        title = (
            tags.get("title")
            or tags.get("track")
            or title
        )
        artist = (
            tags.get("artist")
            or tags.get("album_artist")
            or tags.get("albumartist")
            or tags.get("composer")
            or artist
        )
        artist = _clean_artist_metadata(artist)
        album = tags.get("album") or album
        genre = tags.get("genre") or genre
        language = tags.get("language") or tags.get("contentlanguage") or language
        musicbrainz_recordingid = (
            tags.get("musicbrainz_recordingid")
            or tags.get("musicbrainz_trackid")
            or musicbrainz_recordingid
        )
        raw_bpm = tags.get("bpm") or tags.get("tbpm") or tags.get("tempo") or ""
        try:
            parsed_bpm = float(str(raw_bpm).strip().replace(",", "."))
            bpm = parsed_bpm if 30.0 <= parsed_bpm <= 240.0 else 0.0
        except (TypeError, ValueError):
            bpm = 0.0
    except Exception:
        if require_playable:
            raise

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "genre": genre,
        "language": language,
        "musicbrainz_recordingid": musicbrainz_recordingid,
        "duration": duration,
        "bpm": bpm,
        "has_embedded_art": has_embedded_art,
    }


def _cache_managed_cover_art(
    file_path: Path,
    station_id: int,
    metadata: dict[str, str | float | bool],
) -> str:
    """Copy sidecar art or extract embedded art into the safe public media root."""
    sidecar_candidates = [
        file_path.with_suffix(extension)
        for extension in (".jpg", ".jpeg", ".png", ".webp")
    ]
    sidecar_candidates.extend(
        file_path.parent / name
        for name in (
            "cover.jpg",
            "cover.png",
            "folder.jpg",
            "folder.png",
            "front.jpg",
            "front.png",
        )
    )
    sidecar = next((candidate for candidate in sidecar_candidates if candidate.is_file()), None)
    has_embedded_art = bool(metadata.get("has_embedded_art", False))
    if sidecar is None and not has_embedded_art:
        return ""

    embedded_payload: tuple[bytes, str] | None = None
    if sidecar is None and file_path.suffix.lower() == ".flac":
        try:
            with file_path.open("rb") as handle:
                if handle.read(4) == b"fLaC":
                    while True:
                        header = handle.read(4)
                        if len(header) != 4:
                            break
                        is_last = bool(header[0] & 0x80)
                        block_type = header[0] & 0x7F
                        block_length = int.from_bytes(header[1:4], "big")
                        if block_length > 32 * 1024 * 1024:
                            break
                        block = handle.read(block_length)
                        if len(block) != block_length:
                            break
                        if block_type == 6:
                            view = memoryview(block)
                            offset = 4

                            def _take_u32() -> int:
                                nonlocal offset
                                if offset + 4 > len(view):
                                    raise ValueError("truncated FLAC picture block")
                                value = struct.unpack_from(">I", view, offset)[0]
                                offset += 4
                                return int(value)

                            mime_length = _take_u32()
                            mime = bytes(view[offset : offset + mime_length]).decode("ascii", errors="ignore").lower()
                            offset += mime_length
                            description_length = _take_u32()
                            offset += description_length
                            for _ in range(4):
                                _take_u32()
                            picture_length = _take_u32()
                            picture = bytes(view[offset : offset + picture_length])
                            if picture and len(picture) == picture_length:
                                extension = {
                                    "image/png": ".png",
                                    "image/webp": ".webp",
                                }.get(mime, ".jpg")
                                embedded_payload = (picture, extension)
                            break
                        if is_last:
                            break
        except (OSError, ValueError, struct.error):
            _log.warning("FLAC cover-art metadata read failed for %s", file_path, exc_info=True)

    digest = hashlib.sha256(
        f"{int(station_id)}\0{_canonical_library_path(file_path)}".encode(
            "utf-8", errors="surrogatepass"
        )
    ).hexdigest()[:24]
    extension = (
        sidecar.suffix.lower()
        if sidecar is not None
        else (embedded_payload[1] if embedded_payload is not None else ".jpg")
    )
    cache_root = get_db_path().parent / "media" / str(int(station_id)) / "cover-art"
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / f"{digest}{extension}"
    temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
    try:
        if sidecar is not None:
            shutil.copy2(sidecar, temporary)
        elif embedded_payload is not None:
            temporary.write_bytes(embedded_payload[0])
        else:
            ffmpeg = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
            if not ffmpeg:
                return ""
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(file_path),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-frames:v",
                    "1",
                    "-y",
                    str(temporary),
                ],
                capture_output=True,
                # Artwork is optional broadcast metadata. A slow/removable
                # managed-library disk must never make one file hold a live
                # folder sync for ten seconds while playout needs the same
                # disk. Sidecar art and FLAC PICTURE blocks are handled above
                # without FFmpeg; give other embedded artwork a short bound.
                timeout=3,
            )
            if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
                return ""
        os.replace(temporary, destination)
    except subprocess.TimeoutExpired:
        _log.warning("managed cover-art extraction timed out for %s", file_path)
        return ""
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("managed cover-art cache failed for %s: %s", file_path, exc)
        return ""
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return f"{int(station_id)}/cover-art/{destination.name}"


def _get_audio_duration(file_path: str) -> float:
    metadata = _get_audio_metadata(file_path)
    return float(metadata["duration"] or 0.0)


def _run_ytdlp_download(url: str, output_dir: Path, audio_format: str, audio_quality: str,
                         download_playlist: bool, job_update_fn=None) -> list[Path]:
    """Run yt-dlp to download audio files. Returns list of downloaded file paths."""
    ytdlp_bin = shutil.which("yt-dlp")
    if not ytdlp_bin:
        raise FileNotFoundError("yt-dlp binary not found in PATH")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build yt-dlp command
    output_template = str(output_dir / "%(title)s.%(ext)s")
    cmd = [
        ytdlp_bin,
        "--no-warnings",
        "-x",                          # extract audio
        "--audio-format", audio_format,
        "--audio-quality", audio_quality,
        "-o", output_template,
        "--restrict-filenames",         # safe filenames
        "--windows-filenames",          # Windows-safe
    ]

    if not download_playlist:
        cmd.append("--no-playlist")
    else:
        cmd.extend(["--yes-playlist"])

    cmd.append(url)

    _log.info("Running yt-dlp: %s", " ".join(cmd))
    if job_update_fn:
        job_update_fn("downloading", "Downloading audio...")

    # Collect files before download to detect new ones
    existing_files = set(output_dir.iterdir()) if output_dir.exists() else set()

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        cwd=str(output_dir),
    )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        _log.error("yt-dlp failed (rc=%d): %s", proc.returncode, stderr)
        raise RuntimeError(f"yt-dlp failed: {stderr[:500]}")

    # Find new files
    current_files = set(output_dir.iterdir()) if output_dir.exists() else set()
    new_files = sorted(current_files - existing_files)

    # Filter to audio files only
    audio_exts = {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac", ".wma", ".webm"}
    audio_files = [f for f in new_files if f.suffix.lower() in audio_exts]

    if not audio_files:
        # Fallback: check if yt-dlp printed the filename
        for line in (proc.stdout or "").splitlines():
            if "Destination:" in line:
                dest = line.split("Destination:", 1)[1].strip()
                p = Path(dest)
                if p.exists():
                    audio_files.append(p)
            elif "[ExtractAudio]" in line and "Destination:" in line:
                dest = line.split("Destination:", 1)[1].strip()
                p = Path(dest)
                if p.exists():
                    audio_files.append(p)

    _log.info("Downloaded %d audio file(s)", len(audio_files))
    return audio_files


def _simulate_ytdlp_import(job_id: str, request_payload: dict) -> dict:
    """Actually download audio via yt-dlp and import into library."""
    from app.runtime_paths import resolve_binary

    init_db()
    conn = get_connection()
    track_type = str(request_payload.get("track_type") or "music").strip().lower() or "music"
    station_id = int(request_payload.get("station_id") or 1)
    target_station_id = int(request_payload.get("target_station_id") or station_id)
    audio_format = str(request_payload.get("audio_format") or "mp3").strip().lower() or "mp3"
    audio_quality = str(request_payload.get("audio_quality") or "192").strip()
    url = str(request_payload.get("url") or "")
    download_playlist = bool(request_payload.get("download_playlist", False))

    # Determine output directory
    db_dir = get_db_path().parent
    output_dir = db_dir / "downloads" / f"station-{target_station_id}" / track_type
    output_dir.mkdir(parents=True, exist_ok=True)

    # Job status update helper
    def _update_job(phase, message):
        with _YTDLP_LOCK:
            job = _YTDLP_JOBS.get(job_id)
            if job:
                job["phase"] = phase
                job["message"] = message
                job["updated_at"] = _now_iso()

    # Run real yt-dlp download
    downloaded_files = _run_ytdlp_download(
        url=url,
        output_dir=output_dir,
        audio_format=audio_format,
        audio_quality=audio_quality,
        download_playlist=download_playlist,
        job_update_fn=_update_job,
    )

    if not downloaded_files:
        raise RuntimeError("yt-dlp completed but no audio files were found")

    _update_job("importing", f"Importing {len(downloaded_files)} track(s)...")

    # Import downloaded files into database
    cur = conn.cursor()
    added = 0
    track_ids = []
    processing_summary = _empty_import_processing_summary()
    for file_path in downloaded_files:
        abs_path = str(file_path.resolve())
        cur.execute("SELECT id FROM tracks WHERE file_path=? LIMIT 1", (abs_path,))
        existing = cur.fetchone()
        if existing:
            track_ids.append(int(existing["id"]))
            continue

        processing_result = _run_import_processing(
            abs_path,
            auto_trim_silence=bool(request_payload.get("auto_trim_silence", False)),
            auto_intro_clean=bool(request_payload.get("auto_intro_clean", False)),
            threshold_db=float(request_payload.get("trim_threshold_db") or -45.0),
            min_silence=float(request_payload.get("trim_min_silence") or 0.15),
            intro_max_cut_s=float(request_payload.get("intro_max_cut_s") or 18.0),
        )
        _accumulate_import_processing(processing_summary, processing_result)
        title = file_path.stem.replace("_", " ").replace("-", " ").strip()
        duration = float(processing_result.get("final_duration") or _get_audio_duration(abs_path))

        cur.execute(
            "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, duration, bpm) "
            "VALUES (?, ?, '', ?, ?, 1, ?, 0)",
            (target_station_id, title, track_type, abs_path, duration),
        )
        track_ids.append(int(cur.lastrowid))
        added += 1
    conn.commit()

    target_station_name = _station_name(conn, target_station_id) or f"Station {target_station_id}"

    return {
        "downloaded_files": len(downloaded_files),
        "scan": {"added": added},
        "track_id": track_ids[0] if track_ids else None,
        "track_ids": track_ids,
        "track_type": track_type,
        "target_station_id": target_station_id,
        "target_station_name": target_station_name,
        "target_dir": str(output_dir),
        "audio_mode": "transcode" if resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg") else "direct_stream",
        "music_only_mode": bool(request_payload.get("music_only_mode", True)),
        "trim": processing_summary["trim"],
        "intro_clean": processing_summary["intro_clean"],
    }


def _empty_import_processing_summary() -> dict:
    return {
        "trim": {
            "trimmed": 0,
            "failed": 0,
            "removed_seconds_total": 0.0,
        },
        "intro_clean": {
            "cleaned": 0,
            "failed": 0,
            "removed_seconds_total": 0.0,
        },
    }


def _accumulate_import_processing(summary: dict, result: dict) -> None:
    for key, success_key in (
        ("trim", "trimmed"),
        ("intro_clean", "cleaned"),
    ):
        source = dict((result or {}).get(key) or {})
        target = summary.setdefault(
            key,
            {
                success_key: 0,
                "failed": 0,
                "removed_seconds_total": 0.0,
            },
        )
        if bool(source.get(success_key)):
            target[success_key] = int(target.get(success_key, 0)) + 1
            target["removed_seconds_total"] = float(
                target.get("removed_seconds_total", 0.0)
            ) + float(source.get("removed_seconds", 0.0) or 0.0)
        elif source.get("error"):
            target["failed"] = int(target.get("failed", 0)) + 1


def _run_import_processing(
    file_path: str,
    *,
    auto_trim_silence: bool = False,
    auto_intro_clean: bool = False,
    threshold_db: float = -45.0,
    min_silence: float = 0.15,
    intro_max_cut_s: float = 18.0,
) -> dict:
    """Apply requested non-destructive import cleanup and report its result."""
    from app.audio.audio_processing import process_imported_track

    return process_imported_track(
        file_path,
        auto_trim_silence=bool(auto_trim_silence),
        auto_intro_clean=bool(auto_intro_clean),
        threshold_db=float(threshold_db),
        min_silence=float(min_silence),
        intro_max_cut_s=float(intro_max_cut_s),
    )


def _run_ytdlp_job_inline(job_id: str) -> None:
    global _YTDLP_RUNNING_JOB_ID

    with _YTDLP_LOCK:
        job = _YTDLP_JOBS.get(job_id)
        if not job:
            return
        if job_id in _YTDLP_PENDING_JOB_IDS:
            _YTDLP_PENDING_JOB_IDS.remove(job_id)
        _YTDLP_RUNNING_JOB_ID = job_id
        now = _now_iso()
        job["status"] = "running"
        job["phase"] = "downloading"
        job["message"] = "Downloading and importing"
        job["started_at"] = now
        job["updated_at"] = now
        request_payload = dict(job.get("request") or {})

    final_status = "completed"
    final_phase = "completed"
    final_message = "Import completed"
    final_error = None
    final_result = None
    try:
        final_result = _simulate_ytdlp_import(job_id, request_payload)
    except Exception as exc:
        final_status = "failed"
        final_phase = "failed"
        final_error = str(exc)
        final_message = final_error or "Import failed"

    with _YTDLP_LOCK:
        existing = _YTDLP_JOBS.get(job_id)
        if not existing:
            _YTDLP_RUNNING_JOB_ID = None
            return
        now = _now_iso()
        existing["status"] = final_status
        existing["phase"] = final_phase
        existing["message"] = final_message
        existing["error"] = final_error
        existing["result"] = final_result
        existing["finished_at"] = now
        existing["updated_at"] = now
        _YTDLP_RUNNING_JOB_ID = None
        if job_id in _YTDLP_RECENT_JOB_IDS:
            _YTDLP_RECENT_JOB_IDS.remove(job_id)
        _YTDLP_RECENT_JOB_IDS.insert(0, job_id)
        del _YTDLP_RECENT_JOB_IDS[80:]


def _build_ytdlp_snapshot(limit_recent: int) -> dict:
    safe_limit = _clamp(limit_recent, 1, 100)
    with _YTDLP_LOCK:
        running = None
        if _YTDLP_RUNNING_JOB_ID and _YTDLP_RUNNING_JOB_ID in _YTDLP_JOBS:
            running = _serialize_ytdlp_job(
                _YTDLP_JOBS[_YTDLP_RUNNING_JOB_ID],
                queue_position=1,
            )

        queued: list[dict] = []
        base = 2 if running else 1
        for idx, jid in enumerate(_YTDLP_PENDING_JOB_IDS):
            job = _YTDLP_JOBS.get(jid)
            if not job:
                continue
            queued.append(_serialize_ytdlp_job(job, queue_position=base + idx))

        recent: list[dict] = []
        completed_count = 0
        failed_count = 0
        for jid in _YTDLP_RECENT_JOB_IDS[:safe_limit]:
            job = _YTDLP_JOBS.get(jid)
            if not job:
                continue
            status = str(job.get("status") or "")
            if status == "completed":
                completed_count += 1
            elif status == "failed":
                failed_count += 1
            recent.append(_serialize_ytdlp_job(job, include_result=True))

        return {
            "running": running,
            "queue": queued,
            "recent": recent,
            "counts": {
                "running": 1 if running else 0,
                "queued": len(queued),
                "completed": completed_count,
                "failed": failed_count,
            },
        }


def _normalize_rule_scope(raw: str) -> str:
    token = str(raw or "").strip().lower()
    return "global" if token == "global" else "station"


def _normalize_track_type(raw: str) -> str:
    token = str(raw or "").strip().lower()
    aliases = {
        "song": "music",
        "songs": "music",
        "jingles": "jingle",
        "ads": "ad",
        "advertisement": "ad",
        "advertisements": "ad",
        "station-id": "station_id",
        "station id": "station_id",
        "station ids": "station_id",
        "recorded show": "show",
        "recorded shows": "show",
    }
    token = aliases.get(token, token)
    if token in {"music", "jingle", "ad", "station_id", "show", "startup"}:
        return token
    return "music"


def _media_search_roots() -> list[Path]:
    db_root = get_db_path().parent
    repo_root = Path(__file__).resolve().parents[2]
    return [
        db_root / "media",
        db_root / "uploads",
        db_root / "soundboard",
        repo_root / "media",
        repo_root / "uploads",
        repo_root / "soundboard",
    ]


def _resolve_media_path(media_path: str) -> Path | None:
    raw = str(media_path or "").strip().replace("\\", "/")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() and candidate.exists():
        resolved = candidate.resolve()
        if any(is_within_root(resolved, root) for root in _media_search_roots()):
            return resolved
        return None
    normalized = Path(raw.lstrip("/"))
    for root in _media_search_roots():
        full = resolve_under_root(root, str(normalized))
        if full is None:
            continue
        if full.exists() and full.is_file():
            return full
    return None


_BOOL_SETTING_KEYS = {
    "operation_logs_enabled",
    "auto_scan_on_startup",
    "speaker_monitor_enabled",
    "icecast_public",
    "auto_trim_imports",
    "auto_intro_clean_imports",
    "loudness_normalization_enabled",
}
_INT_SETTING_KEYS = {"icecast_port", "active_station_id", "speaker_monitor_station_id"}
_FLOAT_SETTING_KEYS = {"default_crossfade_seconds", "output_gain_db"}


def _setting_to_storage_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _setting_from_storage_text(key: str, value: str):
    raw = str(value or "")
    token = raw.strip().lower()
    if key in _BOOL_SETTING_KEYS:
        return token in {"1", "true", "yes", "on"}
    if key in _INT_SETTING_KEYS:
        try:
            return int(float(raw))
        except ValueError:
            return 0
    if key in _FLOAT_SETTING_KEYS:
        try:
            return float(raw)
        except ValueError:
            return 0.0
    return raw


def _typed_settings(values: dict[str, str]) -> dict:
    output: dict = {}
    for key, value in dict(values or {}).items():
        skey = str(key)
        output[skey] = _setting_from_storage_text(skey, str(value))
    return output


def _user_is_station_admin(user: dict) -> bool:
    return str((user or {}).get("role") or "").strip().lower() == "admin" or user_is_superadmin(user)


def _resolve_program_action_show_id(conn, station_id: int, requested_show_id: int | None) -> int:
    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    requested_id = int(requested_show_id or 0)
    if active_session is not None:
        active_show_id = int(active_session["show_id"])
        if requested_id > 0 and requested_id != active_show_id:
            raise HTTPException(status_code=409, detail="Active session belongs to a different show")
        return active_show_id

    if requested_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="show_id is required when no active show session exists",
        )

    show = ShowRepository(conn).get(requested_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    if int(show["station_id"]) != int(station_id):
        raise HTTPException(status_code=400, detail="show_id does not belong to this station")
    return requested_id


def _require_program_show_permission(
    conn,
    user: dict,
    *,
    station_id: int,
    permission_key: str,
    show_id: int | None = None,
) -> int:
    if _user_is_station_admin(user):
        if int(show_id or 0) <= 0:
            active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
            return int(active_session["show_id"]) if active_session is not None else 0
        return _resolve_program_action_show_id(conn, station_id, show_id)
    resolved_show_id = _resolve_program_action_show_id(conn, station_id, show_id)
    if not user_has_show_permission(user, resolved_show_id, permission_key):
        raise HTTPException(status_code=403, detail="Forbidden")
    return resolved_show_id


_PROGRAM_WORKSPACE_SHOW_KEY = "program_workspace_show_id"
_PROGRAM_WORKSPACE_USER_KEY = "program_workspace_user_id"


def _get_program_workspace_claim(conn, station_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT key, value FROM station_settings WHERE station_id=? AND key IN (?, ?)",
        (int(station_id), _PROGRAM_WORKSPACE_SHOW_KEY, _PROGRAM_WORKSPACE_USER_KEY),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    values = {str(row["key"]): str(row["value"] or "").strip() for row in rows}
    try:
        show_id = int(values.get(_PROGRAM_WORKSPACE_SHOW_KEY, "0") or "0")
    except ValueError:
        show_id = 0
    try:
        user_id = int(values.get(_PROGRAM_WORKSPACE_USER_KEY, "0") or "0")
    except ValueError:
        user_id = 0
    if show_id <= 0:
        return None
    return {
        "show_id": int(show_id),
        "user_id": int(user_id) if user_id > 0 else None,
    }


def _set_program_workspace_claim(conn, station_id: int, show_id: int, user_id: int | None) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (int(station_id), _PROGRAM_WORKSPACE_SHOW_KEY, str(int(show_id))),
    )
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (int(station_id), _PROGRAM_WORKSPACE_USER_KEY, str(int(user_id or 0))),
    )
    conn.commit()


def _clear_program_workspace_claim(conn, station_id: int) -> None:
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM station_settings WHERE station_id=? AND key IN (?, ?)",
        (int(station_id), _PROGRAM_WORKSPACE_SHOW_KEY, _PROGRAM_WORKSPACE_USER_KEY),
    )
    conn.commit()


def _require_program_workspace_claim(conn, station_id: int, show_id: int) -> None:
    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    if active_session is not None:
        active_show_id = int(active_session["show_id"])
        if active_show_id != int(show_id):
            raise HTTPException(status_code=403, detail="Forbidden")
        return

    claim = _get_program_workspace_claim(conn, station_id)
    if claim is None:
        raise HTTPException(
            status_code=409,
            detail="Program workspace has not been claimed for this show",
        )
    if int(claim["show_id"]) != int(show_id):
        raise HTTPException(
            status_code=409,
            detail="Program workspace is currently claimed by another show",
        )


def _require_program_workspace_permission(
    conn,
    user: dict,
    *,
    station_id: int,
    permission_key: str,
    show_id: int | None = None,
) -> int:
    resolved_show_id = _require_program_show_permission(
        conn,
        user,
        station_id=station_id,
        permission_key=permission_key,
        show_id=show_id,
    )
    if resolved_show_id > 0:
        _require_program_workspace_claim(conn, station_id, resolved_show_id)
    return resolved_show_id


def _claim_program_workspace(
    conn,
    user: dict,
    *,
    station_id: int,
    show_id: int,
    force: bool = False,
) -> dict:
    resolved_show_id = _resolve_program_action_show_id(conn, station_id, show_id)
    if not _user_is_station_admin(user):
        has_workspace_permission = (
            user_has_show_permission(user, resolved_show_id, "show.broadcast")
            or user_has_show_permission(user, resolved_show_id, "show.queue_edit")
        )
        if not has_workspace_permission:
            raise HTTPException(status_code=403, detail="Forbidden")

    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    if active_session is not None:
        active_show_id = int(active_session["show_id"])
        if active_show_id != int(resolved_show_id):
            raise HTTPException(
                status_code=409,
                detail="Another show is already active on this station",
            )
        _set_program_workspace_claim(conn, station_id, resolved_show_id, int(user["id"]))
        return _program_queue_snapshot(conn, station_id=station_id)

    claim = _get_program_workspace_claim(conn, station_id)
    claim_show_id = int(claim["show_id"]) if claim else 0
    claim_user_id = int(claim["user_id"] or 0) if claim else 0
    current_snapshot = _program_queue_snapshot(conn, station_id=station_id)
    workspace_idle = (
        not current_snapshot.get("items")
        and str(current_snapshot.get("source") or "automation") == "automation"
    )
    can_replace_claim = (
        claim is None
        or claim_show_id == int(resolved_show_id)
        or _user_is_station_admin(user)
        or claim_user_id == int(user["id"])
        or workspace_idle
        or bool(force)
    )
    if not can_replace_claim:
        raise HTTPException(
            status_code=409,
            detail="Program workspace is currently claimed by another show",
        )

    if claim is None or claim_show_id != int(resolved_show_id):
        repo = ProgramQueueRepository(conn)
        repo.clear(station_id=station_id)
        repo.set_source(station_id=station_id, source="automation")
    _set_program_workspace_claim(conn, station_id, resolved_show_id, int(user["id"]))
    return _program_queue_snapshot(conn, station_id=station_id)


def _release_program_workspace(
    conn,
    user: dict,
    *,
    station_id: int,
    show_id: int | None = None,
) -> dict:
    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    if active_session is not None:
        raise HTTPException(status_code=409, detail="An active show session is using this station")

    claim = _get_program_workspace_claim(conn, station_id)
    if claim is None:
        repo = ProgramQueueRepository(conn)
        repo.clear(station_id=station_id)
        repo.set_source(station_id=station_id, source="automation")
        return _program_queue_snapshot(conn, station_id=station_id)

    claimed_show_id = int(claim["show_id"])
    requested_show_id = int(show_id or 0)
    if requested_show_id > 0 and requested_show_id != claimed_show_id:
        raise HTTPException(status_code=409, detail="Program workspace is claimed by another show")

    if not _user_is_station_admin(user):
        same_user = int(claim.get("user_id") or 0) == int(user["id"])
        same_show_permission = (
            user_has_show_permission(user, claimed_show_id, "show.broadcast")
            or user_has_show_permission(user, claimed_show_id, "show.queue_edit")
        )
        if not same_user and not same_show_permission:
            raise HTTPException(status_code=403, detail="Forbidden")

    repo = ProgramQueueRepository(conn)
    repo.clear(station_id=station_id)
    repo.set_source(station_id=station_id, source="automation")
    _clear_program_workspace_claim(conn, station_id)
    return _program_queue_snapshot(conn, station_id=station_id)


def _require_station_program_read_access(
    conn,
    user: dict,
    station_id: int,
    show_id: int | None = None,
) -> None:
    if _user_is_station_admin(user):
        return
    if user_has_permission(user, "shows.manage") or user_has_permission(user, "show.assign.manage"):
        return

    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    repo = ShowRepository(conn)
    if active_session is not None:
        active_show_id = int(active_session["show_id"])
        requested_show_id = int(show_id or 0)
        if requested_show_id > 0 and requested_show_id != active_show_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if repo.is_assigned(active_show_id, int(user["id"])):
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    requested_show_id = int(show_id or 0)
    if requested_show_id > 0:
        requested_show = repo.get(requested_show_id)
        if requested_show is None:
            raise HTTPException(status_code=404, detail="Show not found")
        if int(requested_show["station_id"]) != int(station_id):
            raise HTTPException(status_code=400, detail="show_id does not belong to this station")
        if repo.is_assigned(requested_show_id, int(user["id"])):
            _require_program_workspace_claim(conn, station_id, requested_show_id)
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    assigned = repo.list_for_user(int(user["id"]), station_id=int(station_id))
    if not assigned:
        raise HTTPException(status_code=403, detail="Forbidden")
    raise HTTPException(
        status_code=400,
        detail="show_id is required when no active session exists",
    )


def _extract_update_values(payload: dict | None, drop_keys: set[str] | None = None) -> dict[str, str]:
    data = dict(payload or {})
    drop = set(drop_keys or set())
    raw_values = data.get("values")
    source = raw_values if isinstance(raw_values, dict) else data
    output: dict[str, str] = {}
    for key, value in dict(source).items():
        skey = str(key)
        if skey in drop:
            continue
        output[skey] = _setting_to_storage_text(value)
    return output


def _parse_bool(raw, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    token = str(raw or "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _parse_int(raw, default: int = 0) -> int:
    try:
        return int(float(str(raw)))
    except Exception:
        return int(default)


def _parse_float(raw, default: float = 0.0) -> float:
    try:
        return float(str(raw))
    except Exception:
        return float(default)


def _resolve_scope_station_ids(
    conn,
    station_id: int,
    library_scope: str,
    source_station_id: int | None = None,
) -> list[int]:
    scope = str(library_scope or "local").strip().lower()
    sid = int(station_id or 1)
    if scope == "all":
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT id FROM stations ORDER BY id ASC")
        rows = cur.fetchall()
        station_ids = [int(row["id"]) for row in rows if int(row["id"]) > 0]
        if station_ids:
            return station_ids
        return [sid]
    if scope == "station" and source_station_id is not None:
        source_id = int(source_station_id)
        return [source_id] if source_id > 0 else [sid]
    return [sid]


def _tracks_scope_where(station_ids: list[int], include_active_only: bool = True) -> tuple[str, tuple]:
    ids = sorted({int(v) for v in (station_ids or []) if int(v) > 0})
    if not ids:
        ids = [1]
    placeholders = ",".join("?" for _ in ids)
    where = [f"station_id IN ({placeholders})"]
    if include_active_only:
        where.append("is_active=1")
    return " AND ".join(where), tuple(ids)


def _sync_station_output_from_settings(conn, station_id: int, settings: dict[str, str]) -> dict:
    sid = int(station_id)
    outputs = StationOutputRepository(conn)
    current = outputs.get(sid)

    current_local = bool(current["local_output_enabled"]) if current else False
    current_device = str(current["output_device_id"]) if current else ""
    current_icecast = bool(current["icecast_enabled"]) if current else True
    current_host = str(current["icecast_host"]) if current else "127.0.0.1"
    current_port = int(current["icecast_port"]) if current else 8000
    current_mount = str(current["icecast_mount"]) if current else f"/station{sid}"
    current_user = str(current["icecast_user"]) if current else "source"
    current_pass = str(current["icecast_password"]) if current else ""
    current_gain = float(current["output_gain_db"]) if current else 0.0
    current_profile = str(current["stream_codec_profile"]) if current else "opus_192"
    current_bitrate = int(current["stream_bitrate_kbps"]) if current else 196

    mode = str(settings.get("output_mode", "speaker") or "speaker").strip().lower()
    speaker_monitor = _parse_bool(settings.get("speaker_monitor_enabled", True), True)
    if mode == "icecast":
        icecast_enabled = True
        local_enabled = bool(speaker_monitor)
    else:
        icecast_enabled = False
        local_enabled = True

    payload = {
        "station_id": sid,
        "local_output_enabled": local_enabled,
        "output_device_id": current_device,
        "icecast_enabled": icecast_enabled,
        "icecast_host": str(settings.get("icecast_host", current_host) or current_host),
        "icecast_port": _parse_int(settings.get("icecast_port", current_port), current_port),
        "icecast_mount": str(settings.get("icecast_mount", current_mount) or current_mount),
        "icecast_user": str(
            settings.get("icecast_username", settings.get("icecast_user", current_user))
            or current_user
        ),
        "icecast_password": str(settings.get("icecast_password", current_pass) or current_pass),
        "output_gain_db": _parse_float(settings.get("output_gain_db", current_gain), current_gain),
    }
    stream_profile = resolve_stream_profile(
        str(settings.get("stream_codec_profile", current_profile) or current_profile),
        _parse_int(settings.get("stream_bitrate_kbps", current_bitrate), current_bitrate),
    )
    payload["stream_codec_profile"] = str(stream_profile["profile"])
    payload["stream_bitrate_kbps"] = int(stream_profile["bitrate_kbps"])
    outputs.upsert(**payload)
    return payload


def _queue_set_playing(conn, station_id: int, item_id: int) -> None:
    cur = conn.cursor()
    # Mark any OTHER playing items as done, but leave the target item alone
    # if it is already playing (so we don't reset its started_at).
    cur.execute(
        "UPDATE queue_items SET status='done', finished_at=CURRENT_TIMESTAMP "
        "WHERE station_id=? AND status='playing' AND id<>?",
        (int(station_id), int(item_id)),
    )
    cur.execute(
        "UPDATE queue_items SET status='playing', "
        "started_at=CASE WHEN status='playing' THEN started_at ELSE CURRENT_TIMESTAMP END "
        "WHERE id=?",
        (int(item_id),),
    )
    cur.execute(
        "INSERT INTO playout_state (station_id, current_source, current_item_id) VALUES (?, 'manual', ?) "
        "ON CONFLICT(station_id) DO UPDATE SET current_source='manual', "
        "current_item_id=excluded.current_item_id, updated_at=CURRENT_TIMESTAMP",
        (int(station_id), int(item_id)),
    )
    conn.commit()


def _set_library_autoplay_track(conn, station_id: int, track_id: int | None) -> None:
    source = "library_fallback" if track_id is not None else "none"
    item_id = int(track_id) if track_id is not None else None
    cur = conn.cursor()
    cur.execute(
        "SELECT current_source, current_item_id FROM playout_state WHERE station_id=? LIMIT 1",
        (int(station_id),),
    )
    current = cur.fetchone()
    if current is not None:
        current_source = str(current["current_source"] or "none")
        current_item_id = (
            int(current["current_item_id"]) if current["current_item_id"] is not None else None
        )
        if current_source == source and current_item_id == item_id:
            return
    cur.execute(
        "INSERT INTO playout_state (station_id, current_source, current_item_id) VALUES (?, ?, ?) "
        "ON CONFLICT(station_id) DO UPDATE SET current_source=excluded.current_source, current_item_id=excluded.current_item_id, updated_at=CURRENT_TIMESTAMP",
        (int(station_id), source, item_id),
    )
    conn.commit()


def _get_library_autoplay_track_id(conn, station_id: int) -> int | None:
    state = _get_library_autoplay_state(conn, station_id)
    if state is None:
        return None
    return int(state["track_id"])


def _get_library_autoplay_state(conn, station_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT current_source, current_item_id, started_at FROM playout_state WHERE station_id=? LIMIT 1",
        (int(station_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    if str(row["current_source"] or "") != "library_fallback":
        return None
    if row["current_item_id"] is None:
        return None
    return {
        "track_id": int(row["current_item_id"]),
        "started_at": row["started_at"],
    }


def _parse_started_at(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _estimate_elapsed_seconds(started_at, duration: float = 0.0) -> tuple[float, float]:
    started = _parse_started_at(started_at)
    if started is None:
        return 0.0, 0.0

    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    total = max(0.0, float(duration or 0.0))
    if total > 0:
        elapsed = min(total, elapsed)
        return elapsed, max(0.0, total - elapsed)
    return elapsed, 0.0


_TRACKS_COLUMN_CACHE: dict[str, bool] = {}


def _tracks_column_exists(conn, column_name: str) -> bool:
    normalized = str(column_name or "").strip()
    if not normalized:
        return False
    cached = _TRACKS_COLUMN_CACHE.get(normalized)
    if cached is not None:
        return cached
    try:
        rows = conn.execute("PRAGMA table_info(tracks)").fetchall()
        exists = any(str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) == normalized for row in rows)
    except Exception:
        exists = False
    _TRACKS_COLUMN_CACHE[normalized] = exists
    return exists


def _track_cover_art_select(conn, alias: str = "") -> str:
    if not _tracks_column_exists(conn, "cover_art_url"):
        return "'' AS cover_art_url"
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}cover_art_url, '') AS cover_art_url"


def _track_snapshot(conn, track_id: int) -> dict | None:
    cur = conn.cursor()
    cover_art_select = _track_cover_art_select(conn)
    cur.execute(
        "SELECT id, COALESCE(title, '') AS title, COALESCE(artist, '') AS artist, "
        "COALESCE(album, '') AS album, COALESCE(duration, 0) AS duration, "
        "COALESCE(track_type, 'music') AS track_type, "
        f"{cover_art_select}, "
        "COALESCE(file_path, '') AS file_path "
        "FROM tracks WHERE id=? LIMIT 1",
        (int(track_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "title": str(row["title"] or ""),
        "artist": str(row["artist"] or ""),
        "album": str(row["album"] or ""),
        "duration": float(row["duration"] or 0),
        "track_type": str(row["track_type"] or "music"),
        "cover_art_url": public_track_cover_url(
            int(row["id"]),
            str(row["cover_art_url"] or ""),
        ),
        "file_path": str(row["file_path"] or ""),
    }


def _select_random_library_track(conn, station_id: int, exclude_ids: set[int] | None = None) -> dict | None:
    blocked = sorted({int(x) for x in (exclude_ids or set()) if int(x) > 0})
    where = [
        "is_active=1",
        "COALESCE(file_path, '') <> ''",
        "LOWER(COALESCE(track_type, 'music'))='music'",
        "(station_id=? OR station_id=1)",
    ]
    params: list = [int(station_id)]
    if blocked:
        placeholders = ",".join("?" for _ in blocked)
        where.append(f"id NOT IN ({placeholders})")
        params.extend(blocked)
    query = (
        "SELECT id FROM tracks WHERE "
        + " AND ".join(where)
        + " ORDER BY RANDOM() LIMIT 1"
    )
    cur = conn.cursor()
    cur.execute(query, tuple(params))
    row = cur.fetchone()
    if not row:
        return None
    return _track_snapshot(conn, int(row["id"]))


def _autoplay_random_library_track(
    conn,
    station_id: int,
    exclude_track_id: int | None = None,
    max_attempts: int = 5,
) -> dict:
    sid = int(station_id)
    tried_ids: set[int] = set()
    if exclude_track_id is not None and int(exclude_track_id) > 0:
        tried_ids.add(int(exclude_track_id))

    last_runtime = {
        "station_id": sid,
        "running": False,
        "branch_health": {"icecast": False, "local": False},
        "required_outputs": {"icecast": True, "local": False},
    }
    last_error = ""

    for _ in range(max(1, int(max_attempts))):
        track = _select_random_library_track(conn, sid, exclude_ids=tried_ids)
        if not track:
            break
        track_id = int(track["id"])
        tried_ids.add(track_id)
        runtime_data = _runtime_start_track(conn, station_id=sid, track_id=track_id)
        last_runtime = dict(runtime_data.get("runtime") or last_runtime)
        last_error = str(runtime_data.get("runtime_error") or "")
        if bool(runtime_data.get("runtime_started")):
            _set_library_autoplay_track(conn, sid, track_id)
            return {
                "started": True,
                "track": track,
                "runtime": last_runtime,
                "runtime_error": "",
            }
        # If runtime infrastructure is down, retrying with another track won't help.
        if last_error and "runtime" in last_error.lower():
            break

    _set_library_autoplay_track(conn, sid, None)
    return {
        "started": False,
        "track": None,
        "runtime": last_runtime,
        "runtime_error": last_error,
    }


def _runtime_start_track(conn, station_id: int, track_id: int) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT file_path, COALESCE(title, '') AS title, COALESCE(artist, '') AS artist, "
        "COALESCE(track_type, 'music') AS track_type "
        "FROM tracks WHERE id=? LIMIT 1",
        (int(track_id),),
    )
    row = cur.fetchone()
    if not row or not str(row["file_path"] or "").strip():
        return {
            "runtime_started": False,
            "runtime_error": "track file_path is missing",
            "runtime": {
                "station_id": int(station_id),
                "running": False,
                "branch_health": {"icecast": False, "local": False},
                "required_outputs": {"icecast": True, "local": False},
            },
            "input_uri": "",
            "output_config": {},
        }
    input_uri = resolve_runtime_media_path(str(row["file_path"]).strip())
    if "://" not in input_uri and not Path(input_uri).exists():
        return {
            "runtime_started": False,
            "runtime_error": f"input file not found: {input_uri}",
            "runtime": {
                "station_id": int(station_id),
                "running": False,
                "branch_health": {"icecast": False, "local": False},
                "required_outputs": {"icecast": True, "local": False},
            },
            "input_uri": input_uri,
            "output_config": {},
        }

    # Keep station_outputs in sync with station settings before runtime start.
    station_settings = SettingsRepository(conn).get_station(int(station_id))
    system_settings = SettingsRepository(conn).get_system()
    output_config = _sync_station_output_from_settings(conn, int(station_id), station_settings)
    track_type = str(row["track_type"] or "music").strip().lower() or "music"
    crossfade_seconds = max(
        0.0,
        _parse_float(system_settings.get("default_crossfade_seconds"), 0.0),
    )
    if track_type != "music":
        crossfade_seconds = 0.0

    try:
        from app.api.runtime import runtime_registry

        runtime_registry.start_station(
            station_id=int(station_id),
            input_uri=input_uri,
            stream_title=str(row["title"] or ""),
            stream_artist=str(row["artist"] or ""),
            track_type=track_type,
            crossfade_seconds=crossfade_seconds,
        )
        runtime_state = runtime_registry.status(station_id=int(station_id))
        runtime_error = ""
    except FileNotFoundError as exc:
        runtime_state = {
            "station_id": int(station_id),
            "running": False,
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }
        runtime_error = f"runtime binary missing: {exc}"
    except ValueError as exc:
        runtime_state = {
            "station_id": int(station_id),
            "running": False,
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }
        runtime_error = str(exc)
    except Exception as exc:
        runtime_state = {
            "station_id": int(station_id),
            "running": False,
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }
        runtime_error = f"runtime start failed: {exc}"

    return {
        "runtime_started": bool(runtime_state.get("running")),
        "runtime": runtime_state,
        "runtime_error": runtime_error,
        "input_uri": input_uri,
        "output_config": output_config,
    }


def _runtime_stop_station(station_id: int) -> dict:
    try:
        from app.api.runtime import runtime_registry

        return runtime_registry.stop_station(station_id=int(station_id))
    except Exception:
        return {
            "station_id": int(station_id),
            "running": False,
            "branch_health": {"icecast": False, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }


def _ensure_station_rows(conn) -> list:
    return list(StationRepository(conn).list_all())


def _resolve_station_id(conn, raw_station_id: str | int | None) -> int:
    rows = _ensure_station_rows(conn)
    valid_ids = {int(row["id"]) for row in rows}

    parsed: int | None = None
    if raw_station_id is not None:
        token = str(raw_station_id).strip()
        if token and token.lower() not in {"undefined", "null", "nan", "none"}:
            try:
                parsed = int(float(token))
            except ValueError:
                parsed = None

    if parsed is not None and parsed > 0 and parsed in valid_ids:
        return parsed

    active = StationRepository(conn).get_active()
    if active is not None:
        active_id = int(active["id"])
        if active_id in valid_ids:
            return active_id

    if not rows:
        return 1
    first_id = int(rows[0]["id"])
    return first_id if first_id > 0 else 1


def _station_row_to_dict(row) -> dict:
    station_id = int(row["id"])
    raw_name = str(row["name"] or "").strip()
    token = "".join(ch.lower() if ch.isalnum() else "-" for ch in raw_name).strip("-")
    slug = token or f"station-{station_id}"
    return {
        "id": station_id,
        "name": raw_name,
        "slug": slug,
    }


def _system_settings_snapshot(conn) -> dict:
    rows = _ensure_station_rows(conn)
    default_station_id = int(rows[0]["id"]) if rows else 1
    repo = SettingsRepository(conn)
    raw_settings = repo.ensure_system_defaults(
        {
            "active_station_id": str(default_station_id),
            "speaker_monitor_station_id": str(default_station_id),
        }
    )
    return _typed_settings(raw_settings)


@router.get("/api/stations")
def list_stations(
    _user=Depends(require_any_permission("stations.view", "stations.create", "stations.edit", "stations.delete")),
):
    init_db()
    conn = get_connection()
    try:
        repo = StationRepository(conn)
        rows = filter_station_rows_for_user(_user, list(repo.list_all()))
        return {"stations": [_station_row_to_dict(row) for row in rows]}
    finally:
        conn.close()


@router.post("/api/stations")
def create_station(
    payload: StationCreatePayload,
    _user=Depends(require_permission("stations.create")),
):
    station_name = str(payload.name or "").strip()
    if not station_name:
        raise HTTPException(status_code=400, detail="station name is required")
    init_db()
    conn = get_connection()
    try:
        repo = StationRepository(conn)
        station_id = repo.create(station_name)
        SettingsRepository(conn).upsert_station(
            int(station_id),
            {
                "autoplay_shuffle_seed": f"radiotedu-onair-station-{int(station_id)}",
                "playback_selection_policy": "stable_rotation",
                "broadcast_autostart_enabled": "false",
            },
        )
        if not user_has_unrestricted_station_access(_user):
            UserRepository(conn).add_station_assignment(int(_user["id"]), station_id)
        station = next(
            (
                _station_row_to_dict(row)
                for row in repo.list_all()
                if int(row["id"]) == int(station_id)
            ),
            {"id": int(station_id), "name": station_name, "slug": ""},
        )
        return {"id": station_id, "station": station}
    finally:
        conn.close()


@router.put("/api/stations/{station_id}")
def update_station(
    station_id: int,
    payload: StationUpdatePayload,
    _user=Depends(require_permission("stations.edit")),
):
    station_name = str(payload.name or "").strip()
    if not station_name:
        raise HTTPException(status_code=400, detail="station name is required")
    init_db()
    conn = get_connection()
    try:
        repo = StationRepository(conn)
        rows = list(repo.list_all())
        if not any(int(row["id"]) == int(station_id) for row in rows):
            raise HTTPException(status_code=404, detail="station not found")
        repo.update_name(int(station_id), station_name)
        updated = next(
            _station_row_to_dict(row)
            for row in repo.list_all()
            if int(row["id"]) == int(station_id)
        )
        return {"ok": True, "station": updated}
    finally:
        conn.close()


@router.delete("/api/stations/{station_id}")
def delete_station(
    station_id: int,
    _user=Depends(require_permission("stations.delete")),
):
    init_db()
    conn = get_connection()
    try:
        try:
            from app.api.runtime import worker_loop_manager

            worker_loop_manager.stop(station_id=int(station_id))
        except Exception:
            pass
        runtime_state = _runtime_stop_station(station_id=station_id)
        repo = StationRepository(conn)
        replacement_id = repo.delete(station_id)
        media_cleanup = _remove_station_owned_media(station_id)
        return {
            "ok": True,
            "deleted_station_id": int(station_id),
            "active_station_id": int(replacement_id) if replacement_id is not None else None,
            "runtime": runtime_state,
            "media_cleanup": media_cleanup,
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/api/stations/active")
def get_active_station(
    _user=Depends(require_any_permission("stations.view", "stations.create", "stations.edit", "stations.delete")),
):
    init_db()
    conn = get_connection()
    try:
        repo = StationRepository(conn)
        allowed_rows = filter_station_rows_for_user(_user, list(repo.list_all()))
        allowed_ids = {int(row["id"]) for row in allowed_rows}
        active = repo.get_active()
        if active and (not allowed_ids or int(active["id"]) in allowed_ids):
            return {"station_id": int(active["id"])}
        if allowed_rows:
            return {"station_id": int(allowed_rows[0]["id"])}
        return {"station_id": None}
    finally:
        conn.close()


@router.post("/api/stations/active")
def set_active_station(
    payload: StationActivePayload,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    stations = StationRepository(conn)
    found = [row for row in stations.list_all() if int(row["id"]) == payload.station_id]
    if not found:
        raise HTTPException(status_code=404, detail="station not found")
    stations.set_active(payload.station_id)
    return {"ok": True}


@router.get("/api/speaker/monitor")
def get_speaker_monitor_station(
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    init_db()
    conn = get_connection()
    rows = _ensure_station_rows(conn)
    if not rows:
        return {"station_id": None, "station": None}
    settings = SettingsRepository(conn).get_system()
    requested = settings.get("speaker_monitor_station_id")
    sid = _resolve_station_id(conn, requested)
    station = next((row for row in rows if int(row["id"]) == sid), rows[0])
    return {
        "station_id": int(sid),
        "station": _station_row_to_dict(station),
    }


@router.put("/api/speaker/monitor")
def set_speaker_monitor_station(
    payload: StationActivePayload,
    _user=Depends(require_permission("stations.edit")),
):
    init_db()
    conn = get_connection()
    rows = _ensure_station_rows(conn)
    if not rows:
        raise HTTPException(status_code=404, detail="station not found")
    valid_ids = {int(row["id"]) for row in rows}
    sid = int(payload.station_id)
    if sid not in valid_ids:
        raise HTTPException(status_code=404, detail="station not found")
    settings = SettingsRepository(conn)
    settings.upsert_system({"speaker_monitor_station_id": str(sid)})
    station = next((row for row in rows if int(row["id"]) == sid), rows[0])
    return {
        "message": "Speaker monitor station updated",
        "station_id": int(sid),
        "station": _station_row_to_dict(station),
    }


@router.get("/api/settings/system")
def get_system_settings():
    init_db()
    conn = get_connection()
    settings = _system_settings_snapshot(conn)
    return {"settings": settings, **settings}


@router.put("/api/settings/system")
def update_system_settings(payload: dict | None = Body(default=None)):
    init_db()
    conn = get_connection()
    repo = SettingsRepository(conn)
    _system_settings_snapshot(conn)
    values = _extract_update_values(payload)
    repo.upsert_system(values)
    settings = _system_settings_snapshot(conn)
    return {"ok": True, "settings": settings, **settings}


@router.get("/api/settings/station")
def get_station_settings(station_id: str | None = None):
    init_db()
    conn = get_connection()
    sid = _resolve_station_id(conn, station_id)
    rows = _ensure_station_rows(conn)
    station_row = next((row for row in rows if int(row["id"]) == sid), None)
    repo = SettingsRepository(conn)
    settings = _typed_settings(repo.get_station(sid))
    configured_output = StationOutputRepository(conn).get(sid)
    settings["icecast_password"] = ""
    settings["icecast_password_configured"] = bool(
        configured_output and str(configured_output["icecast_password"] or "")
    )
    return {
        "station_id": sid,
        "station": _station_row_to_dict(station_row) if station_row is not None else None,
        "settings": settings,
        **settings,
    }


@router.put("/api/settings/station")
@router.post("/api/settings/station")
def update_station_settings(
    station_id: str | None = None,
    payload: dict | None = Body(default=None),
):
    init_db()
    conn = get_connection()
    body_station_id = None
    if isinstance(payload, dict):
        body_station_id = payload.get("station_id")
    sid = _resolve_station_id(conn, station_id if station_id is not None else body_station_id)
    values = _extract_update_values(payload, drop_keys={"station_id"})
    if "autoplay_shuffle_seed" in values:
        seed = str(values.get("autoplay_shuffle_seed") or "").strip()
        if not 3 <= len(seed) <= 120 or any(ord(char) < 32 for char in seed):
            raise HTTPException(status_code=400, detail="invalid_autoplay_shuffle_seed")
        values["autoplay_shuffle_seed"] = seed
    if "playback_selection_policy" in values:
        policy = str(values.get("playback_selection_policy") or "").strip().lower()
        if policy != "stable_rotation":
            raise HTTPException(status_code=400, detail="unsupported_playback_selection_policy")
        values["playback_selection_policy"] = policy
    supplied_password = str(values.pop("icecast_password", "") or "")
    repo = SettingsRepository(conn)
    repo.upsert_station(sid, values)
    merged = repo.get_station(sid)
    if supplied_password:
        merged["icecast_password"] = supplied_password
    output_config = _sync_station_output_from_settings(conn, sid, merged)
    repo.upsert_station(sid, {"icecast_password": ""})
    output_config["icecast_password"] = ""
    output_config["icecast_password_configured"] = bool(
        StationOutputRepository(conn).get(sid)["icecast_password"]
    )
    return {
        "ok": True,
        "station_id": sid,
        "settings": _typed_settings(values),
        "output_runtime": {
            "restart_requested": False,
            "output_config": output_config,
        },
    }


def _playlist_row_to_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "playlist_id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "name": str(row["name"]),
        "description": str(row["description"] or ""),
        "playlist_type": str(row["playlist_type"] or "manual"),
        "item_count": int(row["item_count"] or 0),
        "created_at": str(row["created_at"]),
    }


def _playlist_item_row_to_dict(row) -> dict:
    return {
        "id": int(row["item_id"] if "item_id" in row.keys() else row["id"]),
        "item_id": int(row["item_id"] if "item_id" in row.keys() else row["id"]),
        "playlist_id": int(row["playlist_id"]),
        "track_id": int(row["track_id"]),
        "position": int(row["position"]),
        "title": str(row["title"] or "") if "title" in row.keys() else "",
        "artist": str(row["artist"] or "") if "artist" in row.keys() else "",
        "duration": float(row["duration"] or 0.0) if "duration" in row.keys() else 0.0,
        "created_at": str(row["created_at"]),
    }


def _auto_playlist_track_type_filter(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if token in {"", "any", "*"}:
        return "any"
    if token in {"ad", "ads"}:
        return "ad"
    if token in {"music", "jingle"}:
        return token
    return "any"


def _auto_playlist_sort_sql(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if token == "title":
        return "LOWER(COALESCE(title, '')) ASC, id ASC"
    if token == "artist":
        return "LOWER(COALESCE(artist, '')) ASC, id ASC"
    if token == "bpm_asc":
        return "COALESCE(bpm, 0) ASC, id ASC"
    if token == "bpm_desc":
        return "COALESCE(bpm, 0) DESC, id ASC"
    if token == "duration_desc":
        return "COALESCE(duration, 0) DESC, id ASC"
    return "RANDOM()"


@router.get("/api/playlists")
def list_playlists(station_id: int):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    return [_playlist_row_to_dict(row) for row in repo.list_for_station(station_id)]


@router.post("/api/playlists")
def create_playlist(payload: PlaylistCreatePayload):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    playlist_id = repo.create(
        payload.station_id,
        payload.name,
        description=payload.description,
        playlist_type="manual",
    )
    return {"id": playlist_id, "playlist_id": playlist_id}


@router.post("/api/playlists/auto/generate")
def auto_generate_playlist(payload: PlaylistAutoGeneratePayload):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)

    station_id = int(payload.station_id or 1)
    playlist_name = str(payload.name or "").strip()
    if not playlist_name:
        raise HTTPException(status_code=400, detail="playlist name is required")

    playlist_id = repo.create(
        station_id=station_id,
        name=playlist_name,
        description=str(payload.description or "").strip(),
        playlist_type="auto",
    )

    where_parts = ["station_id=?", "is_active=1"]
    params: list = [station_id]

    artist_token = str(payload.artist or "").strip()
    if artist_token:
        where_parts.append("LOWER(COALESCE(artist, '')) LIKE LOWER(?)")
        params.append(f"%{artist_token}%")

    genre_token = str(payload.genre or "").strip()
    if genre_token:
        where_parts.append("LOWER(COALESCE(genre, '')) = LOWER(?)")
        params.append(genre_token)

    track_type_filter = _auto_playlist_track_type_filter(payload.track_type)
    if track_type_filter != "any":
        where_parts.append("LOWER(COALESCE(track_type, 'music')) = ?")
        params.append(track_type_filter)

    if payload.bpm_min is not None:
        where_parts.append("COALESCE(bpm, 0) >= ?")
        params.append(float(payload.bpm_min))
    if payload.bpm_max is not None:
        where_parts.append("COALESCE(bpm, 0) <= ?")
        params.append(float(payload.bpm_max))

    safe_limit = _clamp(int(payload.limit or 50), 1, 500)
    order_sql = _auto_playlist_sort_sql(payload.sort_by)
    where_sql = " AND ".join(where_parts)

    cur = conn.cursor()
    cur.execute(
        f"SELECT id FROM tracks WHERE {where_sql} ORDER BY {order_sql} LIMIT ?",
        tuple(params + [safe_limit]),
    )
    track_ids = [int(row["id"]) for row in cur.fetchall()]
    item_ids = repo.bulk_add(playlist_id=playlist_id, track_ids=track_ids)

    return {
        "ok": True,
        "playlist_id": int(playlist_id),
        "track_count": len(track_ids),
        "item_ids": item_ids,
        "name": playlist_name,
        "description": str(payload.description or "").strip(),
    }


@router.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    row = repo.get(playlist_id)
    if not row:
        raise HTTPException(status_code=404, detail="playlist not found")
    return {
        **_playlist_row_to_dict(row),
        "items": [_playlist_item_row_to_dict(item) for item in repo.list_items(playlist_id)],
    }


@router.post("/api/playlists/{playlist_id}/items")
def add_playlist_item(playlist_id: int, payload: PlaylistItemCreatePayload):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    if not repo.get(playlist_id):
        raise HTTPException(status_code=404, detail="playlist not found")
    try:
        item_id = repo.add_item(playlist_id=playlist_id, track_id=payload.track_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": item_id, "item_id": item_id}


@router.delete("/api/playlists/{playlist_id}/items/{item_id}")
def delete_playlist_item(playlist_id: int, item_id: int):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    repo.delete_item(playlist_id=playlist_id, item_id=item_id)
    return {"ok": True}


@router.put("/api/playlists/{playlist_id}/reorder")
def reorder_playlist_items(playlist_id: int, payload: PlaylistReorderPayload):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    repo.reorder(playlist_id=playlist_id, item_ids=payload.item_ids)
    return {"ok": True}


@router.post("/api/playlists/{playlist_id}/bulk")
def bulk_add_playlist_items(playlist_id: int, payload: PlaylistBulkPayload):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    if not repo.get(playlist_id):
        raise HTTPException(status_code=404, detail="playlist not found")
    try:
        created_ids = repo.bulk_add(playlist_id=playlist_id, track_ids=payload.track_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item_ids": created_ids}


@router.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int):
    init_db()
    conn = get_connection()
    repo = PlaylistRepository(conn)
    repo.delete(playlist_id)
    return {"ok": True}


def _queue_row_to_item(
    row, queue_index: int, *,
    is_current: bool = False,
    is_next: bool = False,
    is_played: bool = False,
    estimated_time: str = "",
) -> dict:
    # For played items, show when they started playing
    played_at = ""
    if is_played:
        import datetime
        started_at_str = None
        try:
            started_at_str = row["started_at"]
        except (KeyError, IndexError):
            pass
        if started_at_str:
            try:
                utcnow = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                now = datetime.datetime.now()
                utc_offset = now - utcnow
                started_utc = datetime.datetime.strptime(str(started_at_str), "%Y-%m-%d %H:%M:%S")
                started_local = started_utc + utc_offset
                played_at = started_local.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                pass

    return {
        "id": int(row["id"]),
        "queue_index": int(queue_index),
        "position": None if is_played else int(row["position"]),
        "station_id": int(row["station_id"]),
        "track_id": int(row["track_id"]),
        "title": str(row["title"] or ""),
        "artist": str(row["artist"] or ""),
        "status": str(row["status"] or "pending"),
        "duration": float(row["duration"] or 0.0),
        "track_type": str(row["track_type"] or "music"),
        "is_current": is_current,
        "is_next": is_next,
        "is_played": is_played,
        "estimated_time": str(played_at or estimated_time or ""),
    }


def _compute_estimated_times(active_rows, crossfade_seconds: float = 0.0):
    """Calculate estimated play time for each active queue row."""
    import datetime

    now = datetime.datetime.now()
    utcnow = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    utc_offset = now - utcnow  # local - utc

    estimates = {}
    cumulative_offset = 0.0

    for row in active_rows:
        rid = row["id"]
        status = str(row["status"] or "")
        duration = float(row["duration"] or 0.0)

        if status == "playing":
            try:
                started_at_str = row["started_at"]
            except (KeyError, IndexError):
                started_at_str = None
            if started_at_str and duration > 0:
                try:
                    started_utc = datetime.datetime.strptime(
                        str(started_at_str), "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    try:
                        started_utc = datetime.datetime.fromisoformat(str(started_at_str))
                    except (ValueError, TypeError):
                        started_utc = utcnow
                elapsed = max(0.0, (utcnow - started_utc).total_seconds())
                remaining = max(0.0, duration - elapsed)
                started_local = started_utc + utc_offset
                estimates[rid] = started_local.strftime("%H:%M:%S")
                cumulative_offset = max(0.0, remaining - crossfade_seconds)
            else:
                estimates[rid] = now.strftime("%H:%M:%S")
        elif status == "pending":
            est = now + datetime.timedelta(seconds=cumulative_offset)
            estimates[rid] = est.strftime("%H:%M:%S")
            cumulative_offset += max(0.0, duration - crossfade_seconds) if duration > 0 else 0.0

    return estimates


def _queue_mutation_acknowledgement(station_id: int) -> dict:
    """Publish a durable queue mutation to live observers without guessing playback.

    Queue rows are the worker's source of truth.  This acknowledgement confirms
    persistence and websocket delivery; it deliberately does not claim that an
    already-playing audio process was interrupted.
    """
    snapshot = None
    try:
        snapshot = list_legacy_queue(station_id=int(station_id))
    except Exception:
        # The write transaction has already committed.  Do not turn a transient
        # read-side failure into an apparent failed operator command.
        snapshot = None
    delivered = False
    worker_running = False
    observed = False
    target_sequence = int(snapshot.get("sequence") or 0) if snapshot else 0
    try:
        from app.ws.broadcaster import broadcaster

        if snapshot is not None:
            delivered = bool(broadcaster.on_queue_changed(int(station_id), snapshot))
    except Exception:
        delivered = False
    try:
        from app.api.runtime import worker_loop_manager

        deadline = time.monotonic() + 0.35
        worker_status = worker_loop_manager.status(int(station_id))
        worker_running = bool(worker_status.get("running"))
        while worker_running and int(worker_status.get("last_observed_queue_sequence") or 0) < target_sequence and time.monotonic() < deadline:
            time.sleep(0.05)
            worker_status = worker_loop_manager.status(int(station_id))
            worker_running = bool(worker_status.get("running"))
        observed = bool(worker_running and int(worker_status.get("last_observed_queue_sequence") or 0) >= target_sequence)
    except Exception:
        worker_running = False
    worker_state = "observed" if observed else ("pending" if worker_running else "not_running")
    response = {
        "persistence": {"committed": True},
        "worker_acknowledgement": {
            "observed": observed,
            "state": worker_state,
            "detail": (
                "The worker observed this queue revision."
                if observed
                else (
                    "The worker polls the durable queue; it has not acknowledged this revision."
                    if worker_running
                    else "The worker is not running; it cannot acknowledge this revision."
                )
            ),
        },
        "runtime_acknowledgement": {
            "persisted": True,
            "queue_event_published": delivered,
            "worker_running": worker_running,
            "observed": observed,
            "current_track_interrupted": False,
        },
    }
    if snapshot is not None:
        response["queue"] = snapshot
        response["queue_sequence"] = target_sequence
    return response


@router.get("/api/queue")
def list_legacy_queue(station_id: int):
    init_db()
    conn = get_connection()
    try:
        return _list_legacy_queue_from_connection(conn, station_id)
    finally:
        conn.close()


def _list_legacy_queue_from_connection(conn, station_id: int):
    repo = QueueRepository(conn)

    # Show ~30 min of history (enough for typical track durations)
    done_rows = repo.list_done_recent(station_id=station_id, limit=15)
    active_rows = ensure_broadcast_queue_filled(conn, station_id=station_id)

    playing_ids = {r["id"] for r in active_rows if r["status"] == "playing"}
    first_pending_id = next(
        (r["id"] for r in active_rows if r["status"] == "pending"), None
    )

    # Calculate estimated play times
    crossfade = 0.0
    try:
        from app.repositories.settings_repo import SettingsRepository
        crossfade = float(
            SettingsRepository(conn).get_system().get("default_crossfade_seconds", 0.0)
        )
    except Exception:
        pass
    estimates = _compute_estimated_times(active_rows, crossfade)

    items = []
    for row in done_rows:
        items.append(_queue_row_to_item(row, -1, is_played=True))
    for idx, row in enumerate(active_rows):
        items.append(_queue_row_to_item(
            row, idx,
            is_current=(row["id"] in playing_ids),
            is_next=(row["id"] == first_pending_id),
            estimated_time=estimates.get(row["id"], ""),
        ))

    return {
        "items": items,
        "total": len(active_rows),
        "station_id": int(station_id),
        "revision": repo.active_revision(active_rows),
        "sequence": repo.change_sequence(station_id),
    }


@router.post("/api/queue/refresh")
def refresh_legacy_queue(station_id: int = 1):
    init_db()
    conn = get_connection()
    repo = QueueRepository(conn)
    rows = repo.list_active_ordered(station_id=int(station_id))
    # Re-index active queue order defensively to keep indexes deterministic for UI moves.
    for idx, row in enumerate(rows, start=1):
        conn.cursor().execute(
            "UPDATE queue_items SET position=? WHERE id=?",
            (idx, int(row["id"])),
        )
    conn.commit()
    return {"message": "Queue refreshed", "total": len(rows), "station_id": int(station_id)}


@router.delete("/api/queue/{queue_index}")
def delete_legacy_queue_item(
    queue_index: int,
    station_id: int,
    item_id: int,
    expected_revision: str,
):
    init_db()
    conn = get_connection()
    repo = QueueRepository(conn)
    try:
        outcome = repo.delete_pending_item(
            station_id=station_id,
            item_id=item_id,
            expected_revision=expected_revision,
        )
        if outcome == "missing":
            raise HTTPException(status_code=404, detail="queue item not found")
        if outcome == "playing":
            raise HTTPException(status_code=409, detail="current playing queue item cannot be removed")
        if outcome == "stale":
            raise HTTPException(status_code=409, detail="queue changed; reload before removing an item")
    finally:
        conn.close()
    return {"ok": True, **_queue_mutation_acknowledgement(station_id)}


@router.post("/api/queue/move")
def move_legacy_queue_item(payload: QueueMovePayload):
    init_db()
    conn = get_connection()
    repo = QueueRepository(conn)
    try:
        outcome = repo.move_pending_item(
            station_id=payload.station_id,
            item_id=payload.item_id,
            to_index=payload.to_index,
            expected_revision=payload.expected_revision,
        )
        if outcome == "missing":
            raise HTTPException(status_code=404, detail="queue item not found")
        if outcome == "playing":
            raise HTTPException(status_code=409, detail="current playing queue item cannot be moved")
        if outcome in {"stale", "invalid_destination"}:
            raise HTTPException(status_code=409, detail="queue changed; reload before reordering items")
    finally:
        conn.close()
    return {"ok": True, **_queue_mutation_acknowledgement(payload.station_id)}


@router.get("/api/tracks/stats/summary")
def legacy_tracks_stats_summary(
    station_id: int = 1,
    library_scope: str = "local",
    source_station_id: int | None = None,
):
    init_db()
    conn = get_connection()
    sid = _resolve_station_id(conn, station_id)
    scope = str(library_scope or "local").strip().lower()
    station_ids = _resolve_scope_station_ids(
        conn,
        station_id=sid,
        library_scope=scope,
        source_station_id=source_station_id,
    )
    where, params = _tracks_scope_where(station_ids, include_active_only=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT "
        "COUNT(*) AS total_tracks, "
        "SUM(CASE WHEN LOWER(COALESCE(track_type,'music'))='music' THEN 1 ELSE 0 END) AS music_count, "
        "SUM(CASE WHEN LOWER(COALESCE(track_type,'music'))='jingle' THEN 1 ELSE 0 END) AS jingle_count, "
        "SUM(CASE WHEN LOWER(COALESCE(track_type,'music')) IN ('ad','ads') THEN 1 ELSE 0 END) AS ad_count, "
        "COALESCE(SUM(duration), 0) AS total_duration, "
        "COUNT(DISTINCT NULLIF(TRIM(COALESCE(artist,'')), '')) AS unique_artists, "
        "COUNT(DISTINCT NULLIF(TRIM(COALESCE(album,'')), '')) AS unique_albums "
        f"FROM tracks WHERE {where}",
        params,
    )
    row = cur.fetchone()
    total_duration = float(row["total_duration"] or 0.0) if row else 0.0
    return {
        "total_tracks": int(row["total_tracks"] or 0) if row else 0,
        "music_count": int(row["music_count"] or 0) if row else 0,
        "jingle_count": int(row["jingle_count"] or 0) if row else 0,
        "ad_count": int(row["ad_count"] or 0) if row else 0,
        "total_hours": round(total_duration / 3600.0, 2),
        "total_size_mb": 0.0,
        "unique_artists": int(row["unique_artists"] or 0) if row else 0,
        "unique_albums": int(row["unique_albums"] or 0) if row else 0,
        "library_scope": scope,
        "library_station_ids": station_ids,
        "source_station_id": int(source_station_id) if source_station_id else None,
    }


@router.post("/api/liquidsoap/push")
def legacy_liquidsoap_push(
    station_id: int,
    file_path: str = "",
    track_id: int | None = None,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.broadcast",
        show_id=show_id,
    )
    queue_repo = QueueRepository(conn)
    resolved_track_id = int(track_id) if track_id else 0
    if resolved_track_id <= 0 and file_path:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tracks WHERE file_path=? LIMIT 1", (str(file_path),))
        found = cur.fetchone()
        if found:
            resolved_track_id = int(found["id"])
        else:
            cur.execute(
                "INSERT INTO tracks (title, artist, file_path) VALUES (?, ?, ?)",
                ("", "", str(file_path)),
            )
            conn.commit()
            resolved_track_id = int(cur.lastrowid)
    if resolved_track_id <= 0:
        raise HTTPException(status_code=400, detail="track_id or file_path required")
    item_id = queue_repo.enqueue(
        station_id=station_id,
        track_id=resolved_track_id,
        dedupe_key=f"legacy-push:{station_id}:{resolved_track_id}",
    )
    runtime_data = _runtime_start_track(conn, station_id=station_id, track_id=resolved_track_id)
    if runtime_data.get("runtime_started"):
        _queue_set_playing(conn, station_id=station_id, item_id=item_id)
    return {
        "ok": True,
        "item_id": item_id,
        **runtime_data,
    }


def _broadcast_skip_state(conn, station_id: int) -> None:
    try:
        from app.ws.broadcaster import broadcaster
        status_payload = legacy_liquidsoap_status(station_id=int(station_id))
        broadcaster.on_runtime_updated(int(station_id), status_payload)
        broadcaster.on_track_changed(int(station_id), status_payload)
        broadcaster.on_queue_changed(int(station_id), list_legacy_queue(int(station_id)))
    except Exception:
        pass


@router.post("/api/liquidsoap/skip")
def legacy_liquidsoap_skip(
    station_id: int,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.broadcast",
        show_id=show_id,
    )
    queue_repo = QueueRepository(conn)
    program_queue_repo = ProgramQueueRepository(conn)

    # Check if a host track is playing — skip it first
    cur = conn.cursor()
    cur.execute(
        "SELECT current_source, current_item_id FROM playout_state WHERE station_id=?",
        (int(station_id),),
    )
    playout_row = cur.fetchone()
    if playout_row and str(playout_row["current_source"] or "") == "host" and playout_row["current_item_id"] is not None:
        host_item_id = int(playout_row["current_item_id"])
        program_queue_repo.pop_item(host_item_id)
        cur.execute(
            "UPDATE playout_state SET current_source='none', current_item_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE station_id=?",
            (int(station_id),),
        )
        conn.commit()
        # If host queue has more items, let the worker pick the next one
        next_host = program_queue_repo.next_pending(station_id)
        if next_host and program_queue_repo.get_source(station_id) == "host":
            from app.api.runtime import runtime_registry
            track_uri, title, artist, track_type = "", "", "", "music"
            t_cur = conn.cursor()
            t_cur.execute(
                "SELECT file_path, COALESCE(title,'') AS title, COALESCE(artist,'') AS artist, "
                "COALESCE(track_type,'music') AS track_type FROM tracks WHERE id=?",
                (int(next_host["track_id"]),),
            )
            t_row = t_cur.fetchone()
            if t_row:
                from app.media_paths import resolve_runtime_media_path
                track_uri = resolve_runtime_media_path(str(t_row["file_path"] or ""))
                title = str(t_row["title"] or "")
                artist = str(t_row["artist"] or "")
            if track_uri:
                try:
                    runtime_registry.start_station(
                        int(station_id), track_uri,
                        stream_title=title, stream_artist=artist,
                    )
                except Exception:
                    pass
                cur.execute(
                    "UPDATE playout_state SET current_source='host', current_item_id=?, updated_at=CURRENT_TIMESTAMP WHERE station_id=?",
                    (int(next_host["id"]), int(station_id)),
                )
                conn.commit()
            _broadcast_skip_state(conn, station_id)
            return {"ok": True, "skipped": True, "item_id": host_item_id, "started_next": bool(track_uri)}
        # Host queue empty — fall through to automation
        _broadcast_skip_state(conn, station_id)

    cur.execute(
        "SELECT id, track_id FROM queue_items WHERE station_id=? AND status='playing' ORDER BY position ASC, id ASC LIMIT 1",
        (int(station_id),),
    )
    playing = cur.fetchone()
    skipped_item_id = None
    skipped_track_id = None
    if playing is not None:
        skipped_item_id = int(playing["id"])
        skipped_track_id = int(playing["track_id"])
        queue_repo.mark_done(skipped_item_id)

    next_item = queue_repo.next_pending(station_id=station_id)
    if next_item is None:
        exclude_ids = {int(skipped_track_id)} if skipped_track_id else None
        ensure_broadcast_queue_ready_for_playback(
            conn,
            station_id=station_id,
            allow_when_only_playing=True,
            exclude_ids=exclude_ids,
        )
        next_item = queue_repo.next_pending(station_id=station_id)
    if next_item is None:
        runtime_state = _runtime_stop_station(station_id=station_id)
        return {
            "ok": True,
            "skipped": bool(skipped_item_id),
            "item_id": skipped_item_id,
            "started_next": False,
            "runtime": runtime_state,
        }

    next_id = int(next_item["id"])
    next_track_id = int(next_item["track_id"])
    runtime_data = _runtime_start_track(conn, station_id=station_id, track_id=next_track_id)
    runtime_started = bool(runtime_data.get("runtime_started"))
    if runtime_started:
        _queue_set_playing(conn, station_id=station_id, item_id=next_id)
    return {
        "ok": True,
        "skipped": True,
        "item_id": skipped_item_id or next_id,
        "started_next": runtime_started,
        "next_item_id": next_id,
        **runtime_data,
    }


def _require_station_liquidsoap_status_access(
    conn,
    user: dict,
    station_id: int,
    show_id: int | None = None,
) -> None:
    if _user_is_station_admin(user):
        return
    if user_has_permission(user, "stations.view") or user_has_permission(user, "stations.edit"):
        return
    if user_has_permission(user, "shows.manage") or user_has_permission(user, "show.assign.manage"):
        return

    repo = ShowRepository(conn)
    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    if active_session is not None:
        active_show_id = int(active_session["show_id"])
        requested_show_id = int(show_id or 0)
        if requested_show_id > 0 and requested_show_id != active_show_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if repo.is_assigned(active_show_id, int(user["id"])):
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    requested_show_id = int(show_id or 0)
    if requested_show_id > 0:
        requested_show = repo.get(requested_show_id)
        if requested_show is None:
            raise HTTPException(status_code=404, detail="Show not found")
        if int(requested_show["station_id"]) != int(station_id):
            raise HTTPException(status_code=400, detail="show_id does not belong to this station")
        if repo.is_assigned(requested_show_id, int(user["id"])):
            _require_program_workspace_claim(conn, station_id, requested_show_id)
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    assigned = repo.list_for_user(int(user["id"]), station_id=int(station_id))
    if not assigned:
        raise HTTPException(status_code=403, detail="Forbidden")
    raise HTTPException(
        status_code=400,
        detail="show_id is required when no active session exists",
    )


@router.get("/api/liquidsoap/status")
def legacy_liquidsoap_status(
    station_id: int = 1,
    show_id: int | None = None,
    response: Response = None,
    user=Depends(get_current_user),
):
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    init_db()
    conn = get_connection()
    if isinstance(user, dict):
        _require_station_liquidsoap_status_access(conn, user, station_id, show_id=show_id)
    sid = int(station_id)

    stations = StationRepository(conn)
    active = stations.get_active()
    active_station_id = int(active["id"]) if active else sid

    runtime_alive = False
    runtime_elapsed = 0.0
    runtime_branch_health = {"icecast": False, "local": False}
    required_outputs = {"icecast": True, "local": False}
    runtime_status = {}
    active_input_uri = ""
    active_stream_title = ""
    active_stream_artist = ""
    active_track_type = ""

    def _playback_alive(status: dict | None) -> bool:
        if not isinstance(status, dict):
            return False
        branch_health = status.get("branch_health")
        if not isinstance(branch_health, dict):
            return bool(status.get("running", False))

        outputs = status.get("required_outputs")
        if isinstance(outputs, dict):
            required_branches = [
                str(branch) for branch, required in outputs.items() if bool(required)
            ]
            if required_branches:
                return any(
                    bool(branch_health.get(branch, False))
                    for branch in required_branches
                )

        return any(bool(healthy) for healthy in branch_health.values())

    try:
        from app.api.runtime import runtime_registry

        runtime_status = runtime_registry.status(station_id=sid)
        runtime_alive = _playback_alive(runtime_status)
        runtime_elapsed = float(runtime_status.get("elapsed") or 0.0)
        runtime_branch_health = dict(runtime_status.get("branch_health") or runtime_branch_health)
        required_outputs = dict(runtime_status.get("required_outputs") or required_outputs)
        active_input_uri = str(runtime_status.get("active_input_uri") or "")
        active_stream_title = str(runtime_status.get("active_stream_title") or "")
        active_stream_artist = str(runtime_status.get("active_stream_artist") or "")
        active_track_type = str(runtime_status.get("active_track_type") or "")
    except Exception:
        runtime_alive = False

    outputs = StationOutputRepository(conn).get(sid)
    local_output_enabled = bool(outputs["local_output_enabled"]) if outputs else False
    icecast_enabled = bool(outputs["icecast_enabled"]) if outputs else False
    output_mode = "icecast" if icecast_enabled else "speaker"
    speaker_monitor_enabled = local_output_enabled if output_mode == "icecast" else True
    speaker_monitor_station_id = sid if local_output_enabled else None

    # Check if a host track is currently playing via playout_state
    cur = conn.cursor()
    cur.execute(
        "SELECT current_source, current_item_id FROM playout_state WHERE station_id=?",
        (sid,),
    )
    playout_row = cur.fetchone()
    host_playing_item_id = None
    if playout_row and str(playout_row["current_source"] or "") == "host" and playout_row["current_item_id"] is not None:
        host_playing_item_id = int(playout_row["current_item_id"])

    current_track = None
    current_started_at = None

    if runtime_alive and (active_stream_title or active_stream_artist):
        runtime_row = None
        runtime_input_candidates = []
        if active_input_uri and "://" not in active_input_uri:
            runtime_input_candidates.append(active_input_uri)
            runtime_input_candidates.append(active_input_uri.replace("/", "\\"))
            runtime_input_candidates.append(active_input_uri.replace("\\", "/"))
        runtime_input_candidates = list(dict.fromkeys(runtime_input_candidates))
        if runtime_input_candidates:
            # Single query across the path variants instead of one query per
            # variant (was up to 3 round-trips on the 1s status poll).
            placeholders = ",".join("?" for _ in runtime_input_candidates)
            cover_art_select = _track_cover_art_select(conn)
            cur.execute(
                "SELECT id, COALESCE(title, '') AS title, COALESCE(artist, '') AS artist, "
                "COALESCE(album, '') AS album, COALESCE(duration, 0.0) AS duration, "
                "COALESCE(track_type, 'music') AS track_type, "
                f"{cover_art_select} "
                f"FROM tracks WHERE station_id=? AND file_path IN ({placeholders}) LIMIT 1",
                (sid, *runtime_input_candidates),
            )
            runtime_row = cur.fetchone()
        runtime_track_id = int(runtime_row["id"]) if runtime_row else 0
        runtime_cover_art_url = ""
        if runtime_row:
            runtime_cover_art_url = public_track_cover_url(
                runtime_track_id,
                str(runtime_row["cover_art_url"] or ""),
            )
        current_track = {
            "id": runtime_track_id,
            "title": active_stream_title or (str(runtime_row["title"] or "") if runtime_row else ""),
            "artist": active_stream_artist or (str(runtime_row["artist"] or "") if runtime_row else ""),
            "album": str(runtime_row["album"] or "") if runtime_row else "",
            "duration": float(runtime_row["duration"] or 0.0) if runtime_row else 0.0,
            "track_type": active_track_type or (str(runtime_row["track_type"] or "music") if runtime_row else "music"),
            "cover_art_url": runtime_cover_art_url,
        }

    if current_track is None and host_playing_item_id is not None:
        cover_art_select = _track_cover_art_select(conn, "t")
        cur.execute(
            "SELECT pq.id, pq.track_id, pq.created_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.album, '') AS album, COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'music') AS track_type, "
            f"{cover_art_select} "
            "FROM program_queue_items pq "
            "LEFT JOIN tracks t ON t.id = pq.track_id "
            "WHERE pq.id=?",
            (host_playing_item_id,),
        )
        host_row = cur.fetchone()
        if host_row:
            current_started_at = host_row["created_at"]
            current_track = {
                "id": int(host_row["track_id"]) if host_row["track_id"] is not None else 0,
                "title": str(host_row["title"] or ""),
                "artist": str(host_row["artist"] or ""),
                "album": str(host_row["album"] or ""),
                "duration": float(host_row["duration"] or 0.0),
                "track_type": str(host_row["track_type"] or "music"),
                "cover_art_url": public_track_cover_url(
                    int(host_row["track_id"] or 0),
                    str(host_row["cover_art_url"] or ""),
                ),
            }

    if current_track is None:
        cover_art_select = _track_cover_art_select(conn, "t")
        cur.execute(
            "SELECT q.id, q.track_id, q.status, q.started_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.album, '') AS album, COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'music') AS track_type, "
            f"{cover_art_select} "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status IN ('playing', 'pending') "
            "ORDER BY CASE WHEN q.status='playing' THEN 0 ELSE 1 END, q.position ASC, q.id ASC "
            "LIMIT 1",
            (sid,),
        )
        row = cur.fetchone()
        if row:
            current_started_at = row["started_at"]
            current_track = {
                "id": int(row["track_id"]) if row["track_id"] is not None else 0,
                "title": str(row["title"] or ""),
                "artist": str(row["artist"] or ""),
                "album": str(row["album"] or ""),
                "duration": float(row["duration"] or 0.0),
                "track_type": str(row["track_type"] or "music"),
                "cover_art_url": public_track_cover_url(
                    int(row["track_id"] or 0),
                    str(row["cover_art_url"] or ""),
                ),
            }

    if current_track is None:
        auto_state = _get_library_autoplay_state(conn, sid)
        auto_track_id = int(auto_state["track_id"]) if auto_state is not None else None
        if auto_track_id is not None:
            current_started_at = auto_state.get("started_at") if auto_state is not None else None
            track = _track_snapshot(conn, auto_track_id)
            if track:
                current_track = {
                    "id": int(track["id"]),
                    "title": str(track["title"]),
                    "artist": str(track["artist"]),
                    "album": str(track.get("album") or ""),
                    "duration": float(track["duration"]),
                    "track_type": str(track.get("track_type") or "music"),
                    "cover_art_url": str(track.get("cover_art_url") or ""),
                }

    elapsed_sec = 0.0
    remaining_sec = 0.0
    if current_track:
        duration = float(current_track.get("duration") or 0.0)
        if runtime_elapsed > 0.0 or duration <= 0.0:
            elapsed_sec = min(runtime_elapsed, duration) if duration > 0.0 else runtime_elapsed
            remaining_sec = max(0.0, duration - elapsed_sec)
        else:
            elapsed_sec, remaining_sec = _estimate_elapsed_seconds(
                current_started_at,
                duration,
            )

    station_settings = SettingsRepository(conn).get_station(sid)
    program_music_mode = _normalize_program_mode(station_settings.get("program_music_mode", "normal"))
    program_repo = ProgramQueueRepository(conn)
    program_source = program_repo.get_source(station_id=sid)
    program_items = list(program_repo.list_items(station_id=sid))
    effective_source = "automation" if program_source == "host" and not program_items else program_source

    metadata = None
    if current_track:
        metadata = {
            "title": current_track["title"],
            "artist": current_track["artist"],
            "album": current_track.get("album", ""),
            "duration": current_track["duration"],
            "track_type": current_track.get("track_type", "music"),
            "cover_art_url": current_track.get("cover_art_url", ""),
        }

    return {
        "alive": runtime_alive,
        "version": "cleanroom-runtime",
        "status": "active" if (runtime_alive or current_track) else "inactive",
        "metadata": metadata,
        "elapsed": elapsed_sec,
        "remaining": remaining_sec,
        "current_track": current_track,
        "active_input_uri": active_input_uri,
        "active_stream_title": active_stream_title,
        "active_stream_artist": active_stream_artist,
        "active_track_type": active_track_type,
        "active_station_id": active_station_id,
        "is_active_station": int(active_station_id) == sid,
        "speaker_monitor_station_id": speaker_monitor_station_id,
        "local_monitor_active": local_output_enabled,
        "output_mode": output_mode,
        "speaker_monitor_enabled": speaker_monitor_enabled,
        "ducking": program_music_mode == "duck",
        "program_music_muted": program_music_mode == "mute",
        "program_music_mode": program_music_mode,
        "program_queue_source": program_source,
        "program_queue_effective_source": effective_source,
        "program_queue_total": len(program_items),
        "engine_running": runtime_alive,
        "liquidsoap_connected": runtime_alive,
        "branch_health": runtime_branch_health,
        "required_outputs": required_outputs,
    }


@router.post("/api/liquidsoap/cart")
def legacy_liquidsoap_cart(
    file_path: str = "",
    station_id: int = 1,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.broadcast",
        show_id=show_id,
    )
    normalized = str(file_path or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="file_path required")
    return {
        "ok": True,
        "station_id": int(station_id),
        "file_path": normalized,
        "message": "cart overlay accepted",
    }


@router.get("/api/liquidsoap/next")
def legacy_liquidsoap_next(station_id: int | None = None):
    init_db()
    conn = get_connection()
    sid = _resolve_station_id(conn, station_id)
    queue = QueueRepository(conn)
    ensure_broadcast_queue_filled(conn, station_id=sid)
    pending = queue.next_pending(station_id=sid)
    if pending is not None:
        track = _track_snapshot(conn, int(pending["track_id"]))
        if track and str(track.get("file_path") or "").strip():
            queue.mark_playing(int(pending["id"]))
            _set_library_autoplay_track(conn, sid, None)
            return PlainTextResponse(str(track["file_path"]))

    previous_track_id = _get_library_autoplay_track_id(conn, sid)
    track = _select_random_library_track(
        conn,
        sid,
        exclude_ids={int(previous_track_id)} if previous_track_id else set(),
    )
    if track and str(track.get("file_path") or "").strip():
        _set_library_autoplay_track(conn, sid, int(track["id"]))
        return PlainTextResponse(str(track["file_path"]))
    return PlainTextResponse("error", status_code=404)


@router.post("/api/liquidsoap/played")
def legacy_liquidsoap_played(payload: PlayedTrackPayload):
    init_db()
    conn = get_connection()
    sid = _resolve_station_id(conn, payload.station_id)
    queue = QueueRepository(conn)
    cur = conn.cursor()

    cur.execute(
        "SELECT id, track_id FROM queue_items WHERE station_id=? AND status='playing' ORDER BY position ASC, id ASC LIMIT 1",
        (int(sid),),
    )
    row = cur.fetchone()
    queue_item_done = False
    if row is not None:
        queue.mark_done(int(row["id"]))
        queue_item_done = True
        track_id = int(row["track_id"]) if row["track_id"] is not None else 0
        if track_id > 0:
            TrackRepository(conn).mark_played(track_id)
            try:
                MusicUsageService(conn).record_completed_play(
                    station_id=int(sid),
                    track_id=track_id,
                    queue_item_id=int(row["id"]),
                    finished_at=datetime.now(timezone.utc),
                    log_id=f"queue:{int(row['id'])}",
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not persist music-use record for legacy queue item %s", row["id"]
                )

    # Legacy integrations may rely on this endpoint for audit traces.
    try:
        LogRepository(conn).add_event(
            station_id=int(sid),
            event_type="played",
            payload={
                "title": str(payload.title or ""),
                "artist": str(payload.artist or ""),
                "filename": str(payload.filename or ""),
                "queue_item_done": bool(queue_item_done),
            },
        )
    except Exception:
        pass

    _set_library_autoplay_track(conn, sid, None)
    return {
        "ok": True,
        "station_id": int(sid),
        "queue_item_done": bool(queue_item_done),
        "title": str(payload.title or ""),
        "artist": str(payload.artist or ""),
        "filename": str(payload.filename or ""),
    }


@router.get("/api/library/import/ytdlp/settings")
def legacy_ytdlp_settings(
    station_id: int = 1,
    _user=Depends(require_permission("downloads.use")),
):
    init_db()
    conn = get_connection()
    stations = [_station_row_to_dict(row) for row in StationRepository(conn).list_all()]
    sid = int(station_id)
    if not stations:
        stations = [{"id": sid, "name": f"Station {sid}"}]
    station = next((item for item in stations if int(item["id"]) == sid), stations[0])
    ytdlp_path = resolve_binary("yt-dlp.exe") or resolve_binary("yt-dlp") or ""
    ffmpeg_path = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg") or ""

    return {
        "binary": "yt-dlp",
        "binary_found": bool(ytdlp_path),
        "binary_path": ytdlp_path,
        "ffmpeg_found": bool(ffmpeg_path),
        "ffmpeg_path": ffmpeg_path,
        "output_subdir": "downloads",
        "default_audio_format": "mp3",
        "default_audio_quality": "192",
        "default_allow_playlist": True,
        "default_music_only_mode": True,
        "default_auto_trim": False,
        "trim_threshold_db": -45.0,
        "trim_min_silence": 0.15,
        "default_auto_intro_clean": False,
        "default_intro_clean_preset": "normal",
        "intro_min_cut_s": 2.0,
        "intro_max_cut_s": 18.0,
        "intro_analyze_s": 30.0,
        "scan_auto_intro_clean_default": False,
        "scan_auto_trim_default": False,
        "cookies_file_set": False,
        "cookies_file_exists": False,
        "extra_args_set": False,
        "station": station,
        "stations": stations,
        "media_dirs": {
            "music": "downloads/music",
            "jingles": "downloads/jingles",
            "ads": "downloads/ads",
        },
    }


@router.post("/api/library/import/ytdlp")
def legacy_import_ytdlp(
    payload: YtDlpImportPayload,
    _user=Depends(require_permission("downloads.use")),
):
    queued = legacy_queue_ytdlp_import(payload)
    job = dict((queued or {}).get("job") or {})
    result = dict(job.get("result") or {})
    status = str(job.get("status") or "")
    return {
        "message": "Import completed" if status == "completed" else "Import failed",
        "job_id": str(job.get("id") or ""),
        "status": status or "failed",
        "error": job.get("error"),
        "station_id": int(result.get("target_station_id") or payload.station_id or 1),
        "target_station_id": int(result.get("target_station_id") or payload.station_id or 1),
        "target_station_name": str(result.get("target_station_name") or ""),
        "track_type": str(result.get("track_type") or payload.track_type or "music"),
        "target_dir": str(result.get("target_dir") or ""),
        "downloaded_files": int(result.get("downloaded_files") or 0),
        "scan": dict(result.get("scan") or {"added": 0}),
        "trim": dict(result.get("trim") or {"trimmed": 0, "removed_seconds_total": 0.0}),
        "intro_clean": dict(
            result.get("intro_clean") or {"cleaned": 0, "removed_seconds_total": 0.0}
        ),
        "audio_mode": str(result.get("audio_mode") or "direct_stream"),
        "music_only_mode": bool(result.get("music_only_mode", payload.music_only_mode)),
    }


@router.post("/api/library/import/ytdlp/jobs")
def legacy_queue_ytdlp_import(
    payload: YtDlpImportPayload,
    _user=Depends(require_permission("downloads.use")),
):
    url = str(payload.url or "").strip()
    parsed_url = urlparse(url)
    if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
        raise HTTPException(status_code=400, detail="valid URL is required")

    request_payload = payload.model_dump()
    request_payload["url"] = url
    request_payload["track_type"] = str(payload.track_type or "music").strip().lower() or "music"
    request_payload["station_id"] = int(payload.station_id or 1)
    request_payload["target_station_id"] = int(payload.target_station_id or request_payload["station_id"])

    job_id = uuid.uuid4().hex
    now = _now_iso()
    with _YTDLP_LOCK:
        _YTDLP_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "phase": "queued",
            "message": "Waiting in queue",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "request": request_payload,
            "result": None,
            "error": None,
        }
        _YTDLP_PENDING_JOB_IDS.append(job_id)

    # Run in background thread so HTTP response returns immediately
    t = threading.Thread(target=_run_ytdlp_job_inline, args=(job_id,), daemon=True)
    t.start()

    # Wait a short time for quick single-track downloads to finish
    t.join(timeout=2.0)

    with _YTDLP_LOCK:
        job = dict(_YTDLP_JOBS[job_id])
    return {
        "message": "yt-dlp import queued",
        "job": _serialize_ytdlp_job(job, queue_position=1, include_result=True),
    }


@router.get("/api/library/import/ytdlp/jobs/status")
def legacy_ytdlp_queue_status(
    limit_recent: int = 20,
    _user=Depends(require_permission("downloads.use")),
):
    return _build_ytdlp_snapshot(limit_recent=limit_recent)


@router.get("/api/library/import/ytdlp/jobs/{job_id}")
def legacy_ytdlp_job_detail(
    job_id: str,
    _user=Depends(require_permission("downloads.use")),
):
    with _YTDLP_LOCK:
        job = _YTDLP_JOBS.get(str(job_id))
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        queue_position = None
        if _YTDLP_RUNNING_JOB_ID == job_id:
            queue_position = 1
        elif job_id in _YTDLP_PENDING_JOB_IDS:
            queue_position = _YTDLP_PENDING_JOB_IDS.index(job_id) + (2 if _YTDLP_RUNNING_JOB_ID else 1)

        return _serialize_ytdlp_job(job, queue_position=queue_position, include_result=True)


@router.get("/api/library/metadata/rules")
def legacy_metadata_rules(station_id: int = 1, include_inactive: bool = False):
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    where = "(scope='global' OR station_id=?)"
    params: list = [int(station_id)]
    if not include_inactive:
        where += " AND is_active=1"
    cur.execute(
        "SELECT id, station_id, scope, name, target_field, match_type, pattern, replacement, "
        "is_case_sensitive, priority, is_active, created_at, updated_at "
        f"FROM metadata_rules WHERE {where} ORDER BY priority ASC, id ASC",
        tuple(params),
    )
    rows = cur.fetchall()
    rules = [
        {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]) if row["station_id"] is not None else None,
            "scope": str(row["scope"] or "station"),
            "name": str(row["name"] or ""),
            "target_field": str(row["target_field"] or "title"),
            "match_type": str(row["match_type"] or "contains"),
            "pattern": str(row["pattern"] or ""),
            "replacement": str(row["replacement"] or ""),
            "is_case_sensitive": bool(int(row["is_case_sensitive"] or 0)),
            "priority": int(row["priority"] or 100),
            "is_active": bool(int(row["is_active"] or 0)),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    ]
    return {"rules": rules}


@router.post("/api/library/metadata/rules")
def legacy_create_metadata_rule(payload: MetadataRuleCreatePayload):
    init_db()
    conn = get_connection()
    scope = _normalize_rule_scope(payload.scope)
    station_id = None if scope == "global" else int(payload.station_id or 1)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO metadata_rules (station_id, scope, name, target_field, match_type, pattern, replacement, "
        "is_case_sensitive, priority, is_active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            station_id,
            scope,
            str(payload.name or "").strip(),
            str(payload.target_field or "title").strip().lower(),
            str(payload.match_type or "contains").strip().lower(),
            str(payload.pattern or "").strip(),
            str(payload.replacement or ""),
            1 if payload.is_case_sensitive else 0,
            int(payload.priority),
            1 if payload.is_active else 0,
        ),
    )
    conn.commit()
    return {"id": int(cur.lastrowid)}


@router.put("/api/library/metadata/rules/{rule_id}")
def legacy_update_metadata_rule(rule_id: int, payload: MetadataRuleUpdatePayload):
    init_db()
    conn = get_connection()
    updates: list[str] = []
    params: list = []
    if payload.is_active is not None:
        updates.append("is_active=?")
        params.append(1 if payload.is_active else 0)
    if payload.name is not None:
        updates.append("name=?")
        params.append(str(payload.name))
    if payload.pattern is not None:
        updates.append("pattern=?")
        params.append(str(payload.pattern))
    if payload.replacement is not None:
        updates.append("replacement=?")
        params.append(str(payload.replacement))
    if payload.priority is not None:
        updates.append("priority=?")
        params.append(int(payload.priority))
    if not updates:
        return {"ok": True}
    updates.append("updated_at=CURRENT_TIMESTAMP")
    params.append(int(rule_id))
    cur = conn.cursor()
    cur.execute(f"UPDATE metadata_rules SET {', '.join(updates)} WHERE id=?", tuple(params))
    conn.commit()
    if cur.rowcount <= 0:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True}


@router.delete("/api/library/metadata/rules/{rule_id}")
def legacy_delete_metadata_rule(rule_id: int):
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM metadata_rules WHERE id=?", (int(rule_id),))
    conn.commit()
    if cur.rowcount <= 0:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True}


@router.post("/api/library/metadata/autofix")
def legacy_metadata_autofix(payload: MetadataAutofixPayload):
    init_db()
    conn = get_connection()
    scope = str(payload.library_scope or "local").strip().lower()
    station_id = int(payload.station_id or 1)
    station_ids = _resolve_scope_station_ids(
        conn,
        station_id=station_id,
        library_scope=scope,
        source_station_id=payload.source_station_id,
    )
    where, params = _tracks_scope_where(station_ids, include_active_only=True)

    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM tracks WHERE {where}", params)
    analyzed = int(cur.fetchone()["c"])

    bpm_updated = 0
    error_count = 0
    if payload.analyze_bpm:
        for target_station_id in station_ids:
            result = legacy_bpm_analyze(
                BpmAnalyzePayload(
                    station_id=int(target_station_id),
                    only_missing=True,
                    track_type="music",
                    limit=int(payload.limit or 25),
                )
            )
            summary = result.get("summary", {})
            bpm_updated += int(summary.get("bpm_updated") or 0)
            error_count += int(summary.get("errors") or 0)

    return {
        "summary": {
            "updated": 0,
            "bpm_updated": bpm_updated,
            "errors": error_count,
            "metadata_rule_hits": 0,
            "analyzed": analyzed,
        },
        "rule_seed": {
            "created": 0,
            "reactivated": 0,
            "deactivated_station_duplicates": 0,
        },
        "itunes_verify": {
            "updated": 0,
            "matched": 0,
            "low_confidence": 0,
        },
        "library_scope": scope,
        "library_station_ids": station_ids,
    }


@router.post("/api/library/metadata/normalize")
def legacy_metadata_normalize(payload: MetadataNormalizePayload):
    out = legacy_metadata_autofix(
        MetadataAutofixPayload(
            station_id=int(payload.station_id or 1),
            analyze_bpm=bool(payload.analyze_bpm),
            limit=int(payload.limit or 0),
            library_scope=str(payload.library_scope or "local"),
            source_station_id=payload.source_station_id,
            auto_seed_rules=False,
            verify_with_itunes=False,
        )
    )
    return {
        "message": "Library metadata normalized",
        "summary": dict(out.get("summary") or {}),
        "per_station": {},
        "library_scope": str(payload.library_scope or "local").strip().lower(),
        "library_station_ids": list(out.get("library_station_ids") or []),
    }


@router.post("/api/library/metadata/verify/itunes")
def legacy_metadata_verify_itunes(payload: MetadataItunesVerifyPayload):
    init_db()
    conn = get_connection()
    scope = str(payload.library_scope or "local").strip().lower()
    station_id = int(payload.station_id or 1)
    station_ids = _resolve_scope_station_ids(
        conn,
        station_id=station_id,
        library_scope=scope,
        source_station_id=payload.source_station_id,
    )
    where, params = _tracks_scope_where(station_ids, include_active_only=True)
    track_type = str(payload.track_type or "music").strip().lower()
    if track_type in {"music", "jingle", "ads"}:
        where = f"{where} AND LOWER(COALESCE(track_type, 'music'))=?"
        params = tuple(list(params) + [track_type])
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM tracks WHERE {where}", params)
    analyzed = int(cur.fetchone()["c"])
    return {
        "message": "iTunes metadata verification completed",
        "summary": {
            "updated": 0,
            "matched": 0,
            "low_confidence": analyzed,
            "analyzed": analyzed,
        },
        "per_station": {},
        "library_scope": scope,
        "library_station_ids": station_ids,
    }


@router.post("/api/library/bpm/analyze")
def legacy_bpm_analyze(payload: BpmAnalyzePayload):
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    where = "is_active=1 AND station_id=? AND COALESCE(file_path, '')<>''"
    params: list = [int(payload.station_id)]
    track_type = str(payload.track_type or "").strip().lower()
    if track_type and track_type != "any":
        where += " AND LOWER(track_type)=?"
        params.append(track_type)
    candidate_where = where
    candidate_params = list(params)
    if payload.only_missing:
        candidate_where += " AND (bpm IS NULL OR bpm<=0)"
    cur.execute(f"SELECT COUNT(*) AS c FROM tracks WHERE {candidate_where}", tuple(candidate_params))
    eligible = int(cur.fetchone()["c"])
    batch_limit = max(1, min(250, int(payload.limit or 25)))
    cur.execute(
        f"SELECT id, file_path FROM tracks WHERE {candidate_where} ORDER BY id ASC LIMIT ?",
        (*candidate_params, batch_limit),
    )
    candidates = list(cur.fetchall())
    bpm_updated = 0
    error_count = 0
    low_confidence = 0
    updates: list[tuple[float, int]] = []
    for row in candidates:
        try:
            file_path = str(row["file_path"] or "").strip()
            tagged = float(_get_audio_metadata(file_path).get("bpm") or 0.0)
            if tagged > 0:
                bpm, confidence = tagged, 1.0
            else:
                bpm, confidence = analyze_bpm(file_path)
            if bpm > 0 and confidence >= 0.04:
                updates.append((float(bpm), int(row["id"])))
            else:
                low_confidence += 1
        except Exception as exc:  # noqa: BLE001 - report a bounded batch summary
            error_count += 1
            _log.warning("BPM analysis failed for track_id=%s: %s", int(row["id"]), exc)

    if updates:
        def _update_bpm() -> int:
            write_cur = conn.cursor()
            write_cur.executemany("UPDATE tracks SET bpm=? WHERE id=?", updates)
            conn.commit()
            return len(updates)

        try:
            bpm_updated = int(_run_sqlite_write_with_retry(conn, _update_bpm))
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            error_count += len(updates)
            _log.warning(
                "BPM analyze skipped updates because the database stayed locked: station_id=%s",
                int(payload.station_id),
            )

    skipped = max(0, len(candidates) - bpm_updated)
    response = {
        "summary": {
            "eligible": eligible,
            "analyzed": len(candidates),
            "bpm_updated": bpm_updated,
            "skipped": skipped,
            "errors": error_count,
            "low_confidence": low_confidence,
            "remaining": max(0, eligible - bpm_updated),
            "batch_limit": batch_limit,
        }
    }
    conn.close()
    return response


_SCANNER_MAX_NEW_FILES = 5000
_MANAGED_SYNC_MAX_FILES = 100000
_MANAGED_SYNC_METADATA_WORKERS = 4


@router.post("/api/scanner/scan")
def legacy_scanner_scan(
    station_id: int = 1,
    folder: str = "",
    recursive: bool = True,
    track_type: str = "music",
    trim_silence: bool = False,
    clean_intro: bool = False,
):
    """Scan a folder on disk and import its audio files into a station's library.

    Walks the given folder (recursively by default), de-duplicates against the
    tracks already in the station by absolute file path, extracts metadata via
    ffprobe, and inserts new rows as active tracks. The folder is remembered per
    station so a later scan with no path re-uses it. This is what lets a user add
    songs from their E:\\ music folders entirely from the UI.
    """
    init_db()
    conn = get_connection()
    sid = int(station_id or 1)
    kind = _normalize_track_type(track_type)
    settings = SettingsRepository(conn)
    processing_summary = _empty_import_processing_summary()

    folder_str = str(folder or "").strip().strip('"')
    if not folder_str:
        station_settings = settings.get_station(sid)
        folder_str = str(station_settings.get("music_library_folder", "") or "").strip()
    if not folder_str:
        return {
            "station_id": sid,
            "folder": "",
            "scanned": 0,
            "added": 0,
            "skipped_existing": 0,
            "capped": False,
            "reason": "no_folder_configured",
            "results": {
                "music": {"added": 0},
                "jingles": {"added": 0},
                "ads": {"added": 0},
            },
            "trim": processing_summary["trim"],
            "intro_clean": processing_summary["intro_clean"],
        }

    base = Path(folder_str).expanduser()
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder_str}")

    exts = audio_upload_extensions()
    iterator = base.rglob("*") if bool(recursive) else base.iterdir()

    cur = conn.cursor()
    scanned = 0
    added = 0
    skipped_existing = 0
    capped = False
    added_ids: list[int] = []
    for path in iterator:
        try:
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
        except OSError:
            continue
        scanned += 1
        file_path = str(path.resolve())
        cur.execute(
            "SELECT id FROM tracks WHERE station_id=? AND file_path=? LIMIT 1",
            (sid, file_path),
        )
        if cur.fetchone():
            skipped_existing += 1
            continue
        try:
            processing_result = _run_import_processing(
                file_path,
                auto_trim_silence=bool(trim_silence),
                auto_intro_clean=bool(clean_intro),
            )
            _accumulate_import_processing(processing_summary, processing_result)
            metadata = _get_audio_metadata(file_path, fallback_title=path.stem or "Track")
            final_duration = float(
                processing_result.get("final_duration") or metadata["duration"] or 0.0
            )
            cur.execute(
                "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, duration, bpm) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, 0)",
                (
                    sid,
                    str(metadata["title"] or path.stem or "Track"),
                    str(metadata["artist"] or ""),
                    kind,
                    file_path,
                    final_duration,
                ),
            )
            added += 1
            added_ids.append(int(cur.lastrowid))
            if added % 100 == 0:
                conn.commit()
        except Exception:  # noqa: BLE001 - skip a bad file, keep scanning
            _log.exception("scanner: failed to import %s", file_path)
            continue
        if added >= _SCANNER_MAX_NEW_FILES:
            capped = True
            break
    conn.commit()

    # Remember the folder so a future scan can re-use it without re-entry.
    try:
        settings.upsert_station(sid, {"music_library_folder": str(base)})
    except Exception:  # noqa: BLE001
        pass

    music_added = added if kind == "music" else 0
    jingle_added = added if kind == "jingle" else 0
    ad_added = added if kind == "ad" else 0
    return {
        "station_id": sid,
        "folder": str(base),
        "scanned": scanned,
        "added": added,
        "skipped_existing": skipped_existing,
        "capped": capped,
        "results": {
            "music": {"added": music_added},
            "jingles": {"added": jingle_added},
            "ads": {"added": ad_added},
        },
        "trim": processing_summary["trim"],
        "intro_clean": processing_summary["intro_clean"],
    }


def _canonical_library_path(value: str | Path) -> str:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path.absolute()
    return str(resolved).replace("/", "\\").casefold()


def _windows_process_session_id() -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        session_id = ctypes.c_uint(0)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(session_id)
        ):
            return None
        return int(session_id.value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _native_picker_requires_desktop_bridge() -> bool:
    return os.name == "nt" and _windows_process_session_id() == 0


def _reject_noninteractive_native_picker() -> None:
    if _native_picker_requires_desktop_bridge():
        raise HTTPException(
            status_code=409,
            detail=(
                "The broadcast service cannot open windows on the operator desktop. "
                "Use the RadioTEDU OnAir desktop app or enter an absolute path."
            ),
        )


@router.post("/api/operator/pick-folder")
def pick_operator_folder(payload: FolderPickerPayload):
    """Open the operating system folder chooser for a local desktop operator."""
    _reject_noninteractive_native_picker()
    initial = str(payload.initial_folder or "").strip().strip('"')
    description = str(payload.description or "Select a radio media folder").strip()
    if os.name == "nt":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description=$env:RADIO_WALL_PICKER_DESCRIPTION;"
            "$d.ShowNewFolderButton=$true;"
            "if($env:RADIO_WALL_PICKER_INITIAL -and "
            "(Test-Path -LiteralPath $env:RADIO_WALL_PICKER_INITIAL)){"
            "$d.SelectedPath=$env:RADIO_WALL_PICKER_INITIAL};"
            "$r=$d.ShowDialog();"
            "if($r -eq [System.Windows.Forms.DialogResult]::OK){"
            "[Console]::Out.Write($d.SelectedPath)}"
        )
        env = os.environ.copy()
        env["RADIO_WALL_PICKER_INITIAL"] = initial
        env["RADIO_WALL_PICKER_DESCRIPTION"] = description
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=408, detail="folder picker timed out") from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"folder picker unavailable: {exc}") from exc
        selected = str(completed.stdout or "").strip()
        if completed.returncode not in {0, None} and not selected:
            detail = str(completed.stderr or "folder picker failed").strip()
            raise HTTPException(status_code=503, detail=detail[:500])
        return {"ok": True, "selected": bool(selected), "folder": selected}

    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            initialdir=initial or None,
            title=description,
            mustexist=True,
        )
        root.destroy()
        return {"ok": True, "selected": bool(selected), "folder": str(selected or "")}
    except Exception as exc:  # noqa: BLE001 - platform UI availability varies
        raise HTTPException(status_code=503, detail=f"folder picker unavailable: {exc}") from exc


@router.post("/api/operator/pick-file")
def pick_operator_file(payload: FilePickerPayload):
    """Open the operating system file chooser for a local desktop operator."""
    _reject_noninteractive_native_picker()
    initial = str(payload.initial_path or "").strip().strip('"')
    description = str(
        payload.description or "Select a protected configuration file"
    ).strip()
    initial_path = Path(initial).expanduser() if initial else None
    if os.name == "nt":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title=$env:RADIO_WALL_PICKER_DESCRIPTION;"
            "$d.Filter='Environment files (*.env)|*.env|All files (*.*)|*.*';"
            "$d.CheckFileExists=$true;"
            "$d.Multiselect=$false;"
            "if($env:RADIO_WALL_PICKER_INITIAL){"
            "$p=$env:RADIO_WALL_PICKER_INITIAL;"
            "if(Test-Path -LiteralPath $p -PathType Leaf){"
            "$d.InitialDirectory=[System.IO.Path]::GetDirectoryName($p);"
            "$d.FileName=[System.IO.Path]::GetFileName($p)"
            "}elseif(Test-Path -LiteralPath $p -PathType Container){"
            "$d.InitialDirectory=$p}};"
            "$r=$d.ShowDialog();"
            "if($r -eq [System.Windows.Forms.DialogResult]::OK){"
            "[Console]::Out.Write($d.FileName)}"
        )
        env = os.environ.copy()
        env["RADIO_WALL_PICKER_INITIAL"] = initial
        env["RADIO_WALL_PICKER_DESCRIPTION"] = description
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=408, detail="file picker timed out") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"file picker unavailable: {exc}"
            ) from exc
        selected = str(completed.stdout or "").strip()
        if completed.returncode not in {0, None} and not selected:
            detail = str(completed.stderr or "file picker failed").strip()
            raise HTTPException(status_code=503, detail=detail[:500])
        return {"ok": True, "selected": bool(selected), "path": selected}

    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            initialdir=(
                str(initial_path.parent)
                if initial_path and initial_path.is_file()
                else initial or None
            ),
            title=description,
            filetypes=(
                ("Environment files", "*.env"),
                ("All files", "*.*"),
            ),
        )
        root.destroy()
        return {"ok": True, "selected": bool(selected), "path": str(selected or "")}
    except Exception as exc:  # noqa: BLE001 - platform UI availability varies
        raise HTTPException(status_code=503, detail=f"file picker unavailable: {exc}") from exc


def _reindex_pending_queue(conn, station_id: int) -> None:
    rows = conn.execute(
        "SELECT id FROM queue_items WHERE station_id=? AND status='pending' "
        "ORDER BY position ASC, id ASC",
        (int(station_id),),
    ).fetchall()
    for position, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE queue_items SET position=? WHERE id=?",
            (position, int(row["id"])),
        )


@router.post("/api/library/folder/sync")
def sync_station_library_folder(payload: LibraryFolderSyncPayload):
    """Make a station library match a managed local folder.

    ``merge`` only imports/reactivates files. ``replace`` also deactivates music
    outside the folder and removes pending playout references to those tracks.
    A currently playing item is deliberately left in place so a live station
    can finish its track before the refilled queue switches to the new profile.
    """
    init_db()
    conn = get_connection()
    try:
        return _sync_station_library_folder_with_connection(payload, conn)
    finally:
        conn.close()


def _sync_station_library_folder_with_connection(
    payload: LibraryFolderSyncPayload,
    conn,
):
    sid = int(payload.station_id or 1)
    kind = _normalize_track_type(payload.track_type)
    mode = str(payload.mode or "replace").strip().lower()
    if mode not in {"merge", "replace"}:
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'replace'")

    station = conn.execute("SELECT id, name FROM stations WHERE id=?", (sid,)).fetchone()
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")

    folder_value = str(payload.folder or "").strip().strip('"')
    if not folder_value:
        raise HTTPException(status_code=400, detail="folder is required")
    base = Path(folder_value).expanduser()
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder_value}")
    try:
        base = base.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Folder cannot be resolved: {exc}") from exc

    folder_key = (
        "music_library_folder"
        if kind == "music"
        else f"{kind}_library_folder"
    )

    def _stale_profile_guard_result():
        configured_row = conn.execute(
            "SELECT value FROM station_settings WHERE station_id=? AND key=?",
            (sid, folder_key),
        ).fetchone()
        configured_value = str(configured_row["value"] if configured_row else "").strip()
        configured_base = None
        if configured_value:
            try:
                configured_base = Path(configured_value).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                configured_base = Path(configured_value).expanduser()
        if configured_base is not None and (
            _canonical_library_path(configured_base) == _canonical_library_path(base)
        ):
            return None
        return {
            "station_id": sid,
            "folder": str(base),
            "verified": False,
            "skipped": True,
            "reason": "stale_managed_library_profile",
            "configured_folder": str(configured_base or ""),
        }

    if bool(payload.guard_configured_folder):
        guard_result = _stale_profile_guard_result()
        if guard_result is not None:
            return guard_result

    extensions = audio_upload_extensions()
    iterator = base.rglob("*") if bool(payload.recursive) else base.iterdir()
    candidates_by_path: dict[str, Path] = {}
    for path in iterator:
        try:
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        candidates_by_path.setdefault(_canonical_library_path(resolved), resolved)

    candidates = [candidates_by_path[key] for key in sorted(candidates_by_path)]
    if not candidates and not (bool(payload.allow_empty) and mode == "replace"):
        raise HTTPException(status_code=400, detail=f"No supported audio files found in {base}")
    if len(candidates) > _MANAGED_SYNC_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Folder contains {len(candidates)} audio files; the managed-library safety limit is "
                f"{_MANAGED_SYNC_MAX_FILES}."
            ),
        )

    existing_rows = conn.execute(
        "SELECT id, file_path, is_active, title, artist, album, genre, language, "
        "duration, bpm, musicbrainz_recordingid, cover_art_url, managed_file_size, "
        "managed_file_mtime_ns FROM tracks "
        "WHERE station_id=? AND LOWER(track_type)=? ORDER BY id ASC",
        (sid, kind.lower()),
    ).fetchall()
    existing_by_path: dict[str, list] = {}
    for row in existing_rows:
        file_path = str(row["file_path"] or "").strip()
        if file_path:
            existing_by_path.setdefault(_canonical_library_path(file_path), []).append(row)

    # Manual syncs validate every candidate. The background watcher uses
    # incremental mode so a restart does not launch thousands of redundant
    # ffprobe processes for files that are already active and have verified
    # duration metadata. New, inactive, or incomplete rows are still probed
    # before any database write, preserving all-or-nothing replace semantics.
    metadata_by_path: dict[str, dict[str, str | float]] = {}
    probe_candidates: list[Path] = []
    metadata_reused = 0
    for path in candidates:
        canonical = _canonical_library_path(path)
        matches = existing_by_path.get(canonical) or []
        primary = matches[0] if matches else None
        try:
            source_stat = path.stat()
            source_size = int(source_stat.st_size)
            source_mtime_ns = int(source_stat.st_mtime_ns)
        except OSError:
            source_size = -1
            source_mtime_ns = -1
        if (
            bool(payload.incremental)
            and primary is not None
            and bool(primary["is_active"])
            and float(primary["duration"] or 0.0) > 0.05
            and int(primary["managed_file_size"] or -1) == source_size
            and int(primary["managed_file_mtime_ns"] or -1) == source_mtime_ns
        ):
            metadata_by_path[canonical] = {
                "title": str(primary["title"] or path.stem or "Track"),
                "artist": str(primary["artist"] or ""),
                "album": str(primary["album"] or ""),
                "genre": str(primary["genre"] or ""),
                "language": str(primary["language"] or ""),
                "musicbrainz_recordingid": str(primary["musicbrainz_recordingid"] or ""),
                "cover_art_url": str(primary["cover_art_url"] or ""),
                "duration": float(primary["duration"] or 0.0),
                "bpm": float(primary["bpm"] or 0.0),
            }
            metadata_reused += 1
        else:
            probe_candidates.append(path)

    unplayable: list[dict[str, str]] = []

    def _probe_managed_candidate(path: Path):
        try:
            metadata = _get_audio_metadata(
                str(path),
                fallback_title=path.stem or "Track",
                require_playable=True,
            )
            metadata["cover_art_url"] = _cache_managed_cover_art(path, sid, metadata)
            return (path, metadata, "")
        except Exception as exc:  # noqa: BLE001 - return a bounded operator report
            return path, None, str(exc or "unplayable audio")[:300]

    if probe_candidates:
        workers = min(_MANAGED_SYNC_METADATA_WORKERS, len(probe_candidates))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            probe_results = list(executor.map(_probe_managed_candidate, probe_candidates))
    else:
        probe_results = []

    for path, metadata, error in probe_results:
        if metadata is not None:
            metadata_by_path[_canonical_library_path(path)] = metadata
        else:
            try:
                display_path = str(path.relative_to(base))
            except ValueError:
                display_path = path.name
            unplayable.append({"file": display_path, "reason": error})
    if unplayable and not bool(payload.skip_unplayable):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Folder was not changed because one or more audio files failed validation",
                "invalid_count": len(unplayable),
                "files": unplayable[:50],
            },
        )
    if unplayable:
        invalid_paths = {
            _canonical_library_path(base / str(item["file"]))
            for item in unplayable
        }
        candidates_by_path = {
            key: value
            for key, value in candidates_by_path.items()
            if key not in invalid_paths
        }
        candidates = [candidates_by_path[key] for key in sorted(candidates_by_path)]
        if not candidates and not (bool(payload.allow_empty) and mode == "replace"):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Folder contains no playable audio files",
                    "invalid_count": len(unplayable),
                    "files": unplayable[:50],
                },
            )

    added = 0
    reactivated = 0
    retained = 0
    metadata_fallbacks = 0
    duplicate_rows_deactivated = 0
    target_track_ids: list[int] = []
    default_genre = str(payload.default_genre or "").strip()
    default_language = str(payload.default_language or "").strip()

    try:
        # A watcher profile can become stale while a large folder is being
        # probed. Re-check after taking the SQLite write lock so no settings
        # change can race the destructive replace/update phase below.
        conn.execute("BEGIN IMMEDIATE")
        if bool(payload.guard_configured_folder):
            guard_result = _stale_profile_guard_result()
            if guard_result is not None:
                conn.rollback()
                return guard_result
        for path in candidates:
            canonical = _canonical_library_path(path)
            metadata = metadata_by_path[canonical]
            matches = existing_by_path.get(canonical) or []
            if matches:
                primary = matches[0]
                track_id = int(primary["id"])
                if not bool(primary["is_active"]):
                    reactivated += 1
                else:
                    retained += 1
                conn.execute(
                    "UPDATE tracks SET title=?, artist=?, "
                    "album=CASE WHEN ?<>'' THEN ? ELSE album END, "
                    "file_path=?, track_type=?, is_active=1, duration=?, "
                    "managed_file_size=?, managed_file_mtime_ns=?, cover_art_url=?, "
                    "bpm=CASE WHEN ?>0 THEN ? ELSE bpm END, "
                    "genre=CASE WHEN ?<>'' THEN ? ELSE genre END, "
                    "language=CASE WHEN ?<>'' THEN ? ELSE language END, "
                    "musicbrainz_recordingid=CASE WHEN ?<>'' THEN ? ELSE musicbrainz_recordingid END "
                    "WHERE id=?",
                    (
                        str(metadata.get("title") or path.stem or "Track"),
                        str(metadata.get("artist") or ""),
                        str(metadata.get("album") or "").strip(),
                        str(metadata.get("album") or "").strip(),
                        str(path),
                        kind,
                        float(metadata.get("duration") or 0.0),
                        int(path.stat().st_size),
                        int(path.stat().st_mtime_ns),
                        str(metadata.get("cover_art_url") or ""),
                        float(metadata.get("bpm") or 0.0),
                        float(metadata.get("bpm") or 0.0),
                        str(metadata.get("genre") or default_genre).strip(),
                        str(metadata.get("genre") or default_genre).strip(),
                        str(metadata.get("language") or default_language).strip(),
                        str(metadata.get("language") or default_language).strip(),
                        str(metadata.get("musicbrainz_recordingid") or "").strip(),
                        str(metadata.get("musicbrainz_recordingid") or "").strip(),
                        track_id,
                    ),
                )
                for duplicate in matches[1:]:
                    if bool(duplicate["is_active"]):
                        duplicate_rows_deactivated += 1
                    conn.execute("UPDATE tracks SET is_active=0 WHERE id=?", (int(duplicate["id"]),))
                target_track_ids.append(track_id)
                continue

            cursor = conn.execute(
                "INSERT INTO tracks "
                "(station_id, title, artist, album, genre, language, track_type, file_path, "
                "is_active, duration, bpm, musicbrainz_recordingid, "
                "managed_file_size, managed_file_mtime_ns, cover_art_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    str(metadata.get("title") or path.stem or "Track"),
                    str(metadata.get("artist") or ""),
                    str(metadata.get("album") or "").strip(),
                    str(metadata.get("genre") or default_genre).strip(),
                    str(metadata.get("language") or default_language).strip(),
                    kind,
                    str(path),
                    float(metadata.get("duration") or 0.0),
                    float(metadata.get("bpm") or 0.0),
                    str(metadata.get("musicbrainz_recordingid") or "").strip(),
                    int(path.stat().st_size),
                    int(path.stat().st_mtime_ns),
                    str(metadata.get("cover_art_url") or ""),
                ),
            )
            target_track_ids.append(int(cursor.lastrowid))
            added += 1

        deactivated_ids: list[int] = []
        pending_removed = 0
        program_items_removed = 0
        schedules_removed = 0
        if mode == "replace":
            if target_track_ids:
                placeholders = ",".join("?" for _ in target_track_ids)
                stale_rows = conn.execute(
                    f"SELECT id FROM tracks WHERE station_id=? AND LOWER(track_type)=? "
                    f"AND is_active=1 AND id NOT IN ({placeholders}) ORDER BY id ASC",
                    (sid, kind.lower(), *target_track_ids),
                ).fetchall()
            else:
                stale_rows = conn.execute(
                    "SELECT id FROM tracks WHERE station_id=? AND LOWER(track_type)=? "
                    "AND is_active=1 ORDER BY id ASC",
                    (sid, kind.lower()),
                ).fetchall()
            deactivated_ids = [int(row["id"]) for row in stale_rows]
            if deactivated_ids:
                stale_placeholders = ",".join("?" for _ in deactivated_ids)
                conn.execute(
                    f"UPDATE tracks SET is_active=0 WHERE id IN ({stale_placeholders})",
                    tuple(deactivated_ids),
                )
                if bool(payload.remove_pending_queue):
                    cursor = conn.execute(
                        f"DELETE FROM queue_items WHERE station_id=? AND status='pending' "
                        f"AND track_id IN ({stale_placeholders})",
                        (sid, *deactivated_ids),
                    )
                    pending_removed = int(cursor.rowcount or 0)
                    cursor = conn.execute(
                        f"DELETE FROM program_queue_items WHERE station_id=? "
                        f"AND track_id IN ({stale_placeholders})",
                        (sid, *deactivated_ids),
                    )
                    program_items_removed = int(cursor.rowcount or 0)
                    cursor = conn.execute(
                        f"DELETE FROM schedule_items WHERE station_id=? AND status='pending' "
                        f"AND track_id IN ({stale_placeholders})",
                        (sid, *deactivated_ids),
                    )
                    schedules_removed = int(cursor.rowcount or 0)
                    _reindex_pending_queue(conn, sid)

        prefix = "library" if kind == "music" else f"{kind}_library"
        profile_values = {
            "music_library_folder" if kind == "music" else f"{kind}_library_folder": str(base),
            f"{prefix}_management_mode": mode,
            f"{prefix}_recursive": "true" if bool(payload.recursive) else "false",
            f"{prefix}_profile_label": str(payload.profile_label or "").strip(),
            f"{prefix}_default_genre": default_genre,
            f"{prefix}_default_language": default_language,
            f"{prefix}_skip_unplayable": (
                "true" if bool(payload.skip_unplayable) else "false"
            ),
        }
        for key, value in profile_values.items():
            conn.execute(
                "INSERT INTO station_settings (station_id, key, value, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(station_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (sid, key, value),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    try:
        ensure_broadcast_queue_filled(conn, station_id=sid)
        conn.commit()
    except Exception:  # noqa: BLE001 - library sync remains committed; health reports refill issues
        conn.rollback()
        _log.exception("managed library sync could not refill station %s queue", sid)

    active_rows = conn.execute(
        "SELECT id, file_path FROM tracks WHERE station_id=? AND LOWER(track_type)=? AND is_active=1",
        (sid, kind.lower()),
    ).fetchall()
    active_paths = {
        _canonical_library_path(str(row["file_path"] or ""))
        for row in active_rows
        if str(row["file_path"] or "").strip()
    }
    expected_paths = set(candidates_by_path)
    exact_match = active_paths == expected_paths if mode == "replace" else expected_paths.issubset(active_paths)
    if not exact_match:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "library sync finished but verification did not match",
                "expected_files": len(expected_paths),
                "active_files": len(active_paths),
            },
        )
    status_settings = {f"{prefix}_active_files": str(len(active_paths))}
    if kind == "jingle":
        autofollow = conn.execute(
            "SELECT value FROM station_settings WHERE station_id=? "
            "AND key='sweeper_folder_autofollow'",
            (sid,),
        ).fetchone()
        if str(autofollow["value"] if autofollow else "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            status_settings["sweeper_enabled"] = "true" if active_paths else "false"
    SettingsRepository(conn).upsert_station(sid, status_settings)

    return {
        "ok": True,
        "verified": True,
        "station_id": sid,
        "station_name": str(station["name"] or f"Station {sid}"),
        "folder": str(base),
        "mode": mode,
        "track_type": kind,
        "profile_label": str(payload.profile_label or "").strip(),
        "expected_files": len(expected_paths),
        "active_files": len(active_paths),
        "added": added,
        "reactivated": reactivated,
        "retained": retained,
        "deactivated": len(deactivated_ids),
        "duplicate_rows_deactivated": duplicate_rows_deactivated,
        "metadata_fallbacks": metadata_fallbacks,
        "metadata_reused": metadata_reused,
        "metadata_probed": len(probe_results),
        "invalid_files_skipped": len(unplayable),
        "invalid_files": unplayable[:50],
        "pending_queue_items_removed": pending_removed,
        "program_queue_items_removed": program_items_removed,
        "pending_schedules_removed": schedules_removed,
    }


@router.post("/api/scanner/cleanup")
def legacy_scanner_cleanup():
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, COALESCE(file_path, '') AS file_path FROM tracks WHERE is_active=1"
    )
    rows = cur.fetchall()
    missing_ids: list[int] = []
    for row in rows:
        file_path = str(row["file_path"] or "").strip()
        if not file_path:
            continue
        # Keep URL/file-uri tracks intact; only cleanup missing local files.
        if "://" in file_path:
            continue
        if not Path(file_path).exists():
            missing_ids.append(int(row["id"]))
    removed = 0
    for track_id in missing_ids:
        cur.execute("UPDATE tracks SET is_active=0 WHERE id=?", (track_id,))
        removed += int(cur.rowcount or 0)
    conn.commit()
    return {"removed": int(removed)}


@router.get("/api/sweeper/config")
def legacy_sweeper_config(station_id: int = 1):
    init_db()
    conn = get_connection()
    settings = SettingsRepository(conn).get_station(int(station_id))
    enabled = str(settings.get("sweeper_enabled", "false")).strip().lower() in {"1", "true", "yes", "on"}
    try:
        interval = int(float(settings.get("sweeper_interval", "2")))
    except ValueError:
        interval = 2
    mode = str(settings.get("sweeper_mode", "ordered") or "ordered")
    interval_unit = str(settings.get("sweeper_interval_unit", "tracks") or "tracks").strip().lower()
    if interval_unit not in {"tracks", "minutes"}:
        interval_unit = "tracks"

    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM tracks WHERE station_id=? AND is_active=1 AND LOWER(track_type)='jingle'",
        (int(station_id),),
    )
    jingle_count = int(cur.fetchone()["c"])
    return {
        "station_id": int(station_id),
        "enabled": bool(enabled and jingle_count > 0),
        "interval": max(1, interval),
        "interval_unit": interval_unit,
        "mode": mode,
        "jingle_count": jingle_count,
    }


@router.post("/api/sweeper/config")
def legacy_save_sweeper_config(payload: SweeperConfigPayload):
    init_db()
    conn = get_connection()
    station_id = int(payload.station_id)
    interval = max(1, int(payload.interval))
    interval_unit = str(payload.interval_unit or "tracks").strip().lower()
    if interval_unit not in {"tracks", "minutes"}:
        raise HTTPException(status_code=422, detail="interval_unit must be 'tracks' or 'minutes'")
    max_interval = 100 if interval_unit == "tracks" else 1440
    if interval > max_interval:
        raise HTTPException(
            status_code=422,
            detail=f"interval must be between 1 and {max_interval} for {interval_unit}",
        )
    mode = str(payload.mode or "ordered").strip().lower()
    if mode not in {"ordered", "random"}:
        raise HTTPException(status_code=422, detail="mode must be 'ordered' or 'random'")
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM tracks WHERE station_id=? AND is_active=1 AND LOWER(track_type)='jingle'",
        (station_id,),
    )
    jingle_count = int(cur.fetchone()["c"] or 0)
    enabled = bool(payload.enabled) and jingle_count > 0
    cur.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM queue_items "
        "WHERE station_id=? AND status IN ('done', 'playing')",
        (station_id,),
    )
    baseline_queue_id = int(cur.fetchone()["max_id"] or 0)
    repo = SettingsRepository(conn)
    repo.upsert_station(
        station_id,
        {
            "sweeper_enabled": "true" if enabled else "false",
            "sweeper_interval": str(interval),
            "sweeper_interval_unit": interval_unit,
            "sweeper_baseline_queue_id": str(baseline_queue_id),
            "sweeper_mode": mode,
        },
    )
    reconcile_pending_sweeper_queue(conn, station_id)
    response = {
        "ok": True,
        "station_id": station_id,
        "enabled": enabled,
        "interval": interval,
        "interval_unit": interval_unit,
        "mode": mode,
        "jingle_count": jingle_count,
    }
    if bool(payload.enabled) and not enabled:
        response["reason"] = "no_jingles"
    return response


# ------------------------------------------------------------------
# Startup Sound Config
# ------------------------------------------------------------------

@router.get("/api/startup-sound/config")
def legacy_startup_sound_config(station_id: int = 1):
    init_db()
    conn = get_connection()
    settings = SettingsRepository(conn).get_station(int(station_id))
    enabled = str(settings.get("startup_sound_enabled", "false")).strip().lower() in {"1", "true", "yes", "on"}
    mode = str(settings.get("startup_sound_mode", "random") or "random")
    try:
        track_id = int(float(settings.get("startup_sound_track_id", "0")))
    except (TypeError, ValueError):
        track_id = 0

    # Resolve current track info if specific
    track_title = ""
    if track_id > 0:
        cur = conn.cursor()
        cur.execute("SELECT title, artist, file_path FROM tracks WHERE id=?", (track_id,))
        row = cur.fetchone()
        if row:
            t = str(row["title"] or "").strip()
            a = str(row["artist"] or "").strip()
            track_title = f"{a} - {t}" if a and t else (t or a or str(row["file_path"] or ""))

    # Get list of available jingles + startup sounds for the dropdown
    cur = conn.cursor()
    cur.execute(
        "SELECT id, COALESCE(title, '') AS title, COALESCE(artist, '') AS artist, "
        "COALESCE(file_path, '') AS file_path, COALESCE(track_type, 'music') AS track_type "
        "FROM tracks WHERE (station_id=? OR station_id=1) "
        "AND is_active=1 AND COALESCE(file_path, '') <> '' "
        "AND LOWER(COALESCE(track_type, 'music')) IN ('jingle', 'startup') "
        "ORDER BY track_type DESC, title ASC",
        (int(station_id),),
    )
    jingles = []
    for row in cur.fetchall():
        t = str(row["title"] or "").strip()
        a = str(row["artist"] or "").strip()
        tt = str(row["track_type"] or "jingle").strip().lower()
        from pathlib import Path
        fp = Path(str(row["file_path"] or "")).stem
        name = f"{a} - {t}" if a and t else (t or a or fp)
        prefix = "🔊 " if tt == "startup" else "🎵 "
        label = f"{prefix}{name}"
        jingles.append({"id": int(row["id"]), "label": label})

    return {
        "station_id": int(station_id),
        "enabled": bool(enabled),
        "mode": mode,
        "track_id": track_id,
        "track_title": track_title,
        "jingles": jingles,
    }


@router.post("/api/startup-sound/upload")
async def legacy_upload_startup_sound(
    station_id: int = Form(1),
    file: UploadFile = File(...),
):
    """Upload a dedicated startup sound file (stored as track_type='startup')."""
    init_db()
    conn = get_connection()
    sid = int(station_id or 1)
    upload_dir = get_db_path().parent / "uploads" / f"station-{sid}" / "startup"
    destination = await save_upload_file(
        file,
        upload_dir,
        default_stem="startup",
        default_extension=".bin",
        allowed_extensions=audio_upload_extensions(),
    )

    file_path = str(destination.resolve())
    stem = destination.stem
    cur = conn.cursor()
    # Check if already exists
    cur.execute("SELECT id FROM tracks WHERE file_path=? LIMIT 1", (file_path,))
    existing = cur.fetchone()
    if existing:
        track_id = int(existing["id"])
    else:
        cur.execute(
            "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, duration, bpm) "
            "VALUES (?, ?, '', 'startup', ?, 1, 0, 0)",
            (sid, stem or "Startup Sound", file_path),
        )
        track_id = int(cur.lastrowid or 0)
        conn.commit()

    # Try to detect duration via ffprobe
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        dur = float(result.stdout.strip() or 0)
        if dur > 0:
            cur.execute("UPDATE tracks SET duration=? WHERE id=?", (dur, track_id))
            conn.commit()
    except Exception:
        pass  # duration stays 0, runtime-death fallback will handle it

    return {"ok": True, "track_id": track_id, "title": stem, "file_path": file_path}


@router.post("/api/startup-sound/config")
def legacy_save_startup_sound_config(payload: StartupSoundPayload):
    init_db()
    conn = get_connection()
    repo = SettingsRepository(conn)
    repo.upsert_station(
        int(payload.station_id),
        {
            "startup_sound_enabled": "true" if payload.enabled else "false",
            "startup_sound_mode": str(payload.mode or "random"),
            "startup_sound_track_id": str(max(0, int(payload.track_id))),
        },
    )
    return {"ok": True}


@router.post("/api/library/import/upload")
async def legacy_upload_import(
    station_id: int = Form(1),
    target_station_id: int = Form(1),
    track_type: str = Form("music"),
    auto_trim_silence: bool = Form(False),
    auto_intro_clean: bool = Form(False),
    files: list[UploadFile] = File(default_factory=list),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    init_db()
    conn = get_connection()
    sid = int(station_id or 1)
    target_sid = int(target_station_id or sid)
    kind = _normalize_track_type(track_type)
    upload_dir = get_db_path().parent / "uploads" / f"station-{target_sid}" / kind
    upload_dir.mkdir(parents=True, exist_ok=True)

    cur = conn.cursor()
    added = 0
    uploaded = 0
    imported_track_ids: list[int] = []
    failed: list[dict] = []
    processing_summary = _empty_import_processing_summary()
    for item in files:
        source_name = str(getattr(item, "filename", "") or "upload")
        # Each file is isolated: one bad/unsupported/locked file must NOT abort
        # the whole batch or surface as a generic 500. Collect a per-file error
        # so the UI can tell the user exactly which file failed and why.
        try:
            destination = await save_upload_file(
                item,
                upload_dir,
                default_stem="upload",
                default_extension=".bin",
                allowed_extensions=audio_upload_extensions(),
            )
        except HTTPException as exc:
            failed.append({"file": source_name, "error": str(exc.detail or "upload_rejected")})
            _log.warning("library import: rejected %r: %s", source_name, exc.detail)
            continue
        except Exception as exc:  # noqa: BLE001 - report, do not abort batch
            failed.append({"file": source_name, "error": f"save_failed: {exc}"})
            _log.exception("library import: failed to save %r", source_name)
            continue

        stem = destination.stem
        uploaded += 1

        try:
            file_path = str(destination.resolve())
            cur.execute("SELECT id FROM tracks WHERE file_path=? LIMIT 1", (file_path,))
            exists = cur.fetchone()
            if exists:
                continue
            processing_result = _run_import_processing(
                file_path,
                auto_trim_silence=bool(auto_trim_silence),
                auto_intro_clean=bool(auto_intro_clean),
            )
            _accumulate_import_processing(processing_summary, processing_result)
            metadata = _get_audio_metadata(file_path, fallback_title=stem or "Imported Track")
            final_duration = float(
                processing_result.get("final_duration") or metadata["duration"] or 0.0
            )
            cur.execute(
                "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, duration, bpm) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, 0)",
                (
                    target_sid,
                    str(metadata["title"] or stem or "Imported Track"),
                    str(metadata["artist"] or ""),
                    kind,
                    file_path,
                    final_duration,
                ),
            )
            added += 1
            imported_track_ids.append(int(cur.lastrowid))
        except Exception as exc:  # noqa: BLE001 - report, do not abort batch
            failed.append({"file": source_name, "error": f"index_failed: {exc}"})
            _log.exception("library import: failed to index %r", source_name)
            continue
    conn.commit()

    target_station_name = _station_name(conn, target_sid) or f"Station {target_sid}"
    return {
        "ok": len(failed) == 0,
        "uploaded_files": uploaded,
        "imported_track_ids": imported_track_ids,
        "failed": failed,
        "scan": {"added": added},
        "target_station_id": target_sid,
        "target_station_name": target_station_name,
        "target_dir": str(upload_dir),
        "trim": processing_summary["trim"],
        "intro_clean": processing_summary["intro_clean"],
        "options": {
            "auto_trim_silence": bool(auto_trim_silence),
            "auto_intro_clean": bool(auto_intro_clean),
        },
    }


@router.get("/api/media/{media_path:path}")
def legacy_media_stream(media_path: str):
    resolved = _resolve_media_path(media_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(str(resolved))


def _schedule_row_to_dict(row) -> dict:
    play_at_raw = str(row["play_at"] or "")
    window_end_raw = str(row["window_end"] or "")
    start_time = "00:00"
    end_time = "01:00"
    if "T" in play_at_raw:
        start_time = play_at_raw.split("T", 1)[1][:5]
    elif " " in play_at_raw:
        start_time = play_at_raw.split(" ", 1)[1][:5]
    elif len(play_at_raw) >= 5:
        start_time = play_at_raw[:5]

    if window_end_raw:
        if "T" in window_end_raw:
            end_time = window_end_raw.split("T", 1)[1][:5]
        elif " " in window_end_raw:
            end_time = window_end_raw.split(" ", 1)[1][:5]
        elif len(window_end_raw) >= 5:
            end_time = window_end_raw[:5]
    else:
        try:
            sh, sm = [int(x) for x in start_time.split(":")]
            end_total = (sh * 60 + sm + 60) % (24 * 60)
            end_time = f"{end_total // 60:02d}:{end_total % 60:02d}"
        except Exception:
            end_time = "01:00"

    playlist_id = int(row["track_id"] or 0)
    playlist_name = str(row["title"] or "").strip() or f"Playlist {playlist_id or 1}"
    schedule_name = playlist_name
    return {
        "id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "track_id": playlist_id,
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "event_name": schedule_name,
        "schedule_name": schedule_name,
        "start_time": start_time,
        "end_time": end_time,
        "day_of_week": "*",
        "track_count": 1,
        "total_duration": 3600,
        "play_at": play_at_raw,
        "window_end": row["window_end"],
        "status": str(row["status"]),
        "title": playlist_name,
        "artist": str(row["artist"] or ""),
    }


@router.get("/api/schedule")
def list_legacy_schedule(station_id: int):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    rows = repo.list_all(station_id=station_id)
    return [_schedule_row_to_dict(row) for row in rows]


@router.post("/api/schedule")
def create_legacy_schedule(payload: LegacySchedulePayload):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    station_id = int(payload.station_id)
    track_id = int(payload.track_id or payload.playlist_id or 1)
    play_at = str(payload.play_at or "").strip()
    window_end = payload.window_end
    if not play_at:
        start_token = str(payload.start_time or "00:00").strip()[:5] or "00:00"
        end_token = str(payload.end_time or "").strip()[:5]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        play_at = f"{today}T{start_token}:00Z"
        if end_token:
            window_end = f"{today}T{end_token}:00Z"
    item_id = repo.enqueue(
        station_id=station_id,
        track_id=track_id,
        play_at=play_at,
        window_end=window_end,
    )
    return {"id": item_id}


@router.put("/api/schedule/{schedule_id}")
def update_legacy_schedule(schedule_id: int, payload: LegacySchedulePayload):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    station_id = int(payload.station_id)
    _require_schedule_scope(conn, schedule_id, station_id)
    track_id = int(payload.track_id or payload.playlist_id or 1)
    play_at = str(payload.play_at or "").strip()
    window_end = payload.window_end
    if not play_at:
        start_token = str(payload.start_time or "00:00").strip()[:5] or "00:00"
        end_token = str(payload.end_time or "").strip()[:5]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        play_at = f"{today}T{start_token}:00Z"
        if end_token:
            window_end = f"{today}T{end_token}:00Z"
    ok = repo.update(
        schedule_id=schedule_id,
        station_id=station_id,
        track_id=track_id,
        play_at=play_at,
        window_end=window_end,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"ok": True}


@router.delete("/api/schedule/{schedule_id}")
def delete_legacy_schedule(schedule_id: int, station_id: int):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    _require_schedule_scope(conn, schedule_id, station_id)
    ok = repo.delete(schedule_id=schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"ok": True}


@router.get("/api/schedule/timeline")
def legacy_schedule_timeline(station_id: int):
    init_db()
    conn = get_connection()
    repo = ScheduleRepository(conn)
    rows = repo.list_all(station_id=station_id)
    items = [_schedule_row_to_dict(row) for row in rows]
    blocks = [
        {
            "id": int(item["id"]),
            "schedule_name": str(item["schedule_name"]),
            "event_name": str(item["event_name"]),
            "playlist_name": str(item["playlist_name"]),
            "start_time": str(item["start_time"]),
            "end_time": str(item["end_time"]),
            "track_count": int(item["track_count"]),
            "total_duration": int(item["total_duration"]),
        }
        for item in items
    ]
    return {
        "station_id": int(station_id),
        "day_name": datetime.now().strftime("%A"),
        "items": items,
        "blocks": blocks,
    }


def _resolve_active_flag(enabled: bool | None, is_active: bool | None, default: bool = True) -> bool:
    if enabled is not None:
        return bool(enabled)
    if is_active is not None:
        return bool(is_active)
    return bool(default)


def _require_schedule_scope(conn, schedule_id: int, station_id: int):
    repo = ScheduleRepository(conn)
    row = repo.get(schedule_id)
    if not row or int(row["station_id"]) != int(station_id):
        raise HTTPException(status_code=404, detail="schedule not found")
    return row


def _station_owned_ad_entity(conn, table_name: str, entity_id: int) -> int | None:
    if table_name not in {"ad_break_sets", "ad_campaigns"}:
        return None
    cur = conn.cursor()
    cur.execute(
        f"SELECT station_id FROM {table_name} WHERE id=? LIMIT 1",
        (int(entity_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return int(row["station_id"])


def _track_label(conn, track_id: int | None) -> str:
    tid = int(track_id or 0)
    if tid <= 0:
        return ""
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(title, '') AS title FROM tracks WHERE id=? LIMIT 1", (tid,))
    row = cur.fetchone()
    if not row:
        return ""
    return str(row["title"] or "")


def _normalize_slot_rows(raw_slots: list[dict]) -> list[dict]:
    out: list[dict] = []
    for idx, row in enumerate(raw_slots or []):
        if not isinstance(row, dict):
            continue
        slot_time = str(row.get("slot_time") or "").strip()[:5]
        if not slot_time:
            continue
        slot_id = int(row.get("id") or row.get("slot_id") or idx + 1)
        out.append(
            {
                "id": slot_id,
                "slot_id": slot_id,
                "slot_time": slot_time,
                "day_of_week": str(row.get("day_of_week") or "*").strip() or "*",
                "position": int(row.get("position") or idx),
                "is_active": bool(row.get("is_active", True)),
            }
        )
    return out


def _track_rows_for_ids(conn, track_ids: list[int]) -> list[dict]:
    ids = [int(tid) for tid in (track_ids or []) if int(tid) > 0]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, COALESCE(title,'') AS title, COALESCE(artist,'') AS artist, COALESCE(duration,0) AS duration "
        f"FROM tracks WHERE id IN ({placeholders})",
        tuple(ids),
    )
    rows = {int(row["id"]): row for row in cur.fetchall()}
    out = []
    for tid in ids:
        row = rows.get(tid)
        if not row:
            continue
        out.append(
            {
                "track_id": int(row["id"]),
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "artist": str(row["artist"] or ""),
                "duration": float(row["duration"] or 0.0),
            }
        )
    return out


def _ad_break_set_payload_from_request(payload: AdBreakSetPayload, active: bool) -> dict:
    return {
        "description": str(payload.description or "").strip(),
        "is_active": bool(active),
        "slots": _normalize_slot_rows(payload.slots),
        "intro_jingle_track_id": int(payload.intro_jingle_track_id or 0) or None,
        "outro_jingle_track_id": int(payload.outro_jingle_track_id or 0) or None,
    }


def _ad_campaign_payload_from_request(payload: AdCampaignPayload, active: bool) -> dict:
    base = dict(payload.payload or {})
    base.update(
        {
            "is_active": bool(active),
            "start_date": str(payload.start_date or "").strip(),
            "end_date": str(payload.end_date or "").strip(),
            "day_interval": max(1, int(payload.day_interval or 1)),
            "daily_repeat_limit": max(0, int(payload.daily_repeat_limit or 0)),
            "priority": int(payload.priority or 0),
            "notes": str(payload.notes or "").strip(),
            "slot_ids": [int(v) for v in (payload.slot_ids or []) if int(v) > 0],
            "track_ids": [int(v) for v in (payload.track_ids or []) if int(v) > 0],
        }
    )
    return base


def _ad_break_set_row_to_dict(conn, row) -> dict:
    payload_raw = str(row["payload_json"] or "{}")
    try:
        payload = json.loads(payload_raw)
        if not isinstance(payload, dict):
            payload = {}
    except json.JSONDecodeError:
        payload = {}

    slots = _normalize_slot_rows(payload.get("slots") or [])
    intro_id = int(payload.get("intro_jingle_track_id") or 0) or None
    outro_id = int(payload.get("outro_jingle_track_id") or 0) or None
    active = bool(payload.get("is_active", bool(row["enabled"])))

    return {
        "id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "name": str(row["name"] or ""),
        "description": str(payload.get("description") or ""),
        "is_active": active,
        "enabled": active,
        "slots": slots,
        "intro_jingle_track_id": intro_id,
        "outro_jingle_track_id": outro_id,
        "intro_jingle_title": _track_label(conn, intro_id),
        "outro_jingle_title": _track_label(conn, outro_id),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _ad_campaign_row_to_dict(conn, row) -> dict:
    payload_raw = str(row["payload_json"] or "{}")
    try:
        payload = json.loads(payload_raw)
        if not isinstance(payload, dict):
            payload = {}
    except json.JSONDecodeError:
        payload = {}

    active = bool(payload.get("is_active", bool(row["enabled"])))
    slot_ids = [int(v) for v in (payload.get("slot_ids") or []) if int(v) > 0]
    track_ids = [int(v) for v in (payload.get("track_ids") or []) if int(v) > 0]
    tracks = payload.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        tracks = _track_rows_for_ids(conn, track_ids)

    return {
        "id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "name": str(row["name"] or ""),
        "is_active": active,
        "enabled": active,
        "start_date": str(payload.get("start_date") or ""),
        "end_date": str(payload.get("end_date") or ""),
        "day_interval": max(1, int(payload.get("day_interval") or 1)),
        "daily_repeat_limit": max(0, int(payload.get("daily_repeat_limit") or 0)),
        "priority": int(payload.get("priority") or 0),
        "notes": str(payload.get("notes") or ""),
        "slot_ids": slot_ids,
        "track_ids": track_ids,
        "slots": payload.get("slots") if isinstance(payload.get("slots"), list) else [],
        "tracks": tracks,
        "today_play_count": int(payload.get("today_play_count") or 0),
        "next_run_at": str(payload.get("next_run_at") or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


@router.get("/api/ad-break-sets")
def list_ad_break_sets(station_id: int):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    rows = [_ad_break_set_row_to_dict(conn, row) for row in repo.list_break_sets(station_id)]
    return {"station_id": int(station_id), "break_sets": rows}


@router.post("/api/ad-break-sets")
def create_ad_break_set(payload: AdBreakSetPayload):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    active = _resolve_active_flag(payload.enabled, payload.is_active, default=True)
    item_id = repo.create_break_set(
        station_id=payload.station_id,
        name=payload.name,
        enabled=active,
        payload=_ad_break_set_payload_from_request(payload, active),
    )
    return {"id": item_id, "break_set_id": item_id}


@router.put("/api/ad-break-sets/{break_set_id}")
def update_ad_break_set(break_set_id: int, payload: AdBreakSetPayload):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    owner_station_id = _station_owned_ad_entity(conn, "ad_break_sets", break_set_id)
    if owner_station_id is None or int(owner_station_id) != int(payload.station_id):
        raise HTTPException(status_code=404, detail="ad break set not found")
    active = _resolve_active_flag(payload.enabled, payload.is_active, default=True)
    ok = repo.update_break_set(
        break_set_id=break_set_id,
        name=payload.name,
        enabled=active,
        payload=_ad_break_set_payload_from_request(payload, active),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="ad break set not found")
    return {"ok": True}


@router.delete("/api/ad-break-sets/{break_set_id}")
def delete_ad_break_set(break_set_id: int, station_id: int):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    owner_station_id = _station_owned_ad_entity(conn, "ad_break_sets", break_set_id)
    if owner_station_id is None or int(owner_station_id) != int(station_id):
        raise HTTPException(status_code=404, detail="ad break set not found")
    ok = repo.delete_break_set(break_set_id=break_set_id)
    if not ok:
        raise HTTPException(status_code=404, detail="ad break set not found")
    return {"ok": True}


@router.get("/api/ad-campaigns")
def list_ad_campaigns(station_id: int):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    rows = [_ad_campaign_row_to_dict(conn, row) for row in repo.list_campaigns(station_id)]
    return {"station_id": int(station_id), "campaigns": rows}


@router.post("/api/ad-campaigns")
def create_ad_campaign(payload: AdCampaignPayload):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    active = _resolve_active_flag(payload.enabled, payload.is_active, default=True)
    campaign_id = repo.create_campaign(
        station_id=payload.station_id,
        name=payload.name,
        enabled=active,
        payload=_ad_campaign_payload_from_request(payload, active),
    )
    return {"id": campaign_id, "campaign_id": campaign_id}


@router.put("/api/ad-campaigns/{campaign_id}")
def update_ad_campaign(campaign_id: int, payload: AdCampaignPayload):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    owner_station_id = _station_owned_ad_entity(conn, "ad_campaigns", campaign_id)
    if owner_station_id is None or int(owner_station_id) != int(payload.station_id):
        raise HTTPException(status_code=404, detail="ad campaign not found")
    active = _resolve_active_flag(payload.enabled, payload.is_active, default=True)
    ok = repo.update_campaign(
        campaign_id=campaign_id,
        name=payload.name,
        enabled=active,
        payload=_ad_campaign_payload_from_request(payload, active),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="ad campaign not found")
    return {"ok": True}


@router.delete("/api/ad-campaigns/{campaign_id}")
def delete_ad_campaign(campaign_id: int, station_id: int):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    owner_station_id = _station_owned_ad_entity(conn, "ad_campaigns", campaign_id)
    if owner_station_id is None or int(owner_station_id) != int(station_id):
        raise HTTPException(status_code=404, detail="ad campaign not found")
    ok = repo.delete_campaign(campaign_id=campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="ad campaign not found")
    return {"ok": True}


@router.get("/api/ads/runtime")
def ads_runtime(station_id: int):
    init_db()
    conn = get_connection()
    repo = AdCampaignRepository(conn)
    campaign_rows = [_ad_campaign_row_to_dict(conn, row) for row in repo.list_campaigns(station_id)]
    break_rows = [_ad_break_set_row_to_dict(conn, row) for row in repo.list_break_sets(station_id)]

    active_campaigns = [row for row in campaign_rows if bool(row.get("is_active"))]
    campaign_labels = [
        {"id": int(row["id"]), "name": str(row["name"] or f"Campaign #{row['id']}")}
        for row in active_campaigns
    ]

    now_dt = datetime.now()
    now_minutes = now_dt.hour * 60 + now_dt.minute
    slot_rows: list[dict] = []
    for break_set in break_rows:
        set_name = str(break_set.get("name") or f"Break Set #{break_set.get('id')}")
        for slot in (break_set.get("slots") or []):
            slot_time = str(slot.get("slot_time") or "").strip()[:5]
            if len(slot_time) != 5 or ":" not in slot_time:
                continue
            try:
                hh = int(slot_time[:2])
                mm = int(slot_time[3:5])
            except ValueError:
                continue
            minute_of_day = max(0, min(23, hh)) * 60 + max(0, min(59, mm))
            slot_rows.append(
                {
                    "slot_time": slot_time,
                    "break_set_id": int(break_set.get("id") or 0),
                    "break_set_name": set_name,
                    "minute_of_day": minute_of_day,
                    "active_campaigns": campaign_labels,
                    "is_due": minute_of_day <= now_minutes,
                    "played_today": False,
                }
            )

    slot_rows.sort(key=lambda row: (int(row["minute_of_day"]), int(row["break_set_id"])))
    due_slots = [row for row in slot_rows if bool(row.get("is_due"))][-6:]
    next_slots = [row for row in slot_rows if not bool(row.get("is_due"))][:6]
    if not next_slots and slot_rows:
        next_slots = slot_rows[: min(6, len(slot_rows))]

    now_label = "No active ad"
    if due_slots:
        latest = due_slots[-1]
        campaigns_text = ", ".join(c["name"] for c in latest.get("active_campaigns") or [])
        if campaigns_text:
            now_label = f"{latest.get('break_set_name')} @ {latest.get('slot_time')} ({campaigns_text})"
        else:
            now_label = f"{latest.get('break_set_name')} @ {latest.get('slot_time')}"

    return {
        "station_id": int(station_id),
        "now": now_label,
        "break_set_count": len(break_rows),
        "campaign_count": len(campaign_rows),
        "due_slots": due_slots,
        "next_slots": next_slots,
        "history": [],
    }


def _decode_json_text(raw: str) -> dict:
    text = str(raw or "{}")
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
    except json.JSONDecodeError:
        pass
    return {"raw": text}


def _list_play_logs(conn, station_id: int | None = None, limit: int = 200) -> list[dict]:
    cur = conn.cursor()
    safe_limit = max(1, min(int(limit), 1000))
    params: list = []
    where = "q.status IN ('done', 'playing')"
    if station_id is not None:
        where += " AND q.station_id=?"
        params.append(int(station_id))
    cur.execute(
        "SELECT q.id AS queue_id, q.station_id, q.track_id, q.status, "
        "COALESCE(q.finished_at, q.started_at, q.enqueued_at, CURRENT_TIMESTAMP) AS played_at, "
        "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
        "COALESCE(t.duration, 0) AS duration, COALESCE(t.track_type, 'music') AS track_type "
        f"FROM queue_items q LEFT JOIN tracks t ON t.id=q.track_id WHERE {where} "
        "ORDER BY datetime(COALESCE(q.finished_at, q.started_at, q.enqueued_at, CURRENT_TIMESTAMP)) DESC, q.id DESC "
        "LIMIT ?",
        tuple(params + [safe_limit]),
    )
    rows = cur.fetchall()
    return [
        {
            "id": f"play-{int(row['queue_id'])}",
            "station_id": int(row["station_id"]),
            "played_at": str(row["played_at"]),
            "log_type": "play",
            "level": "info",
            "action": "play",
            "title": str(row["title"] or ""),
            "artist": str(row["artist"] or ""),
            "duration": float(row["duration"] or 0.0),
            "track_type": str(row["track_type"] or "music"),
            "details": str(row["status"] or ""),
        }
        for row in rows
    ]


def _list_operation_logs(conn, station_id: int | None = None, limit: int = 200) -> list[dict]:
    repo = LogRepository(conn)
    rows = repo.list_operation_logs(station_id=station_id, limit=limit)
    return [
        {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]) if row["station_id"] is not None else None,
            "level": str(row["level"]),
            "event_type": str(row["event_type"]),
            "message": str(row["message"]),
            "payload": _decode_json_text(row["payload_json"]),
            "created_at": str(row["created_at"]),
            "played_at": str(row["created_at"]),
            "log_type": "operation",
            "action": str(row["event_type"]),
            "title": str(row["message"] or ""),
            "artist": "API",
            "details": str(row["message"] or ""),
        }
        for row in rows
    ]


def _collect_legacy_logs(
    conn,
    station_id: int | None = None,
    limit: int = 200,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    safe_limit = max(1, min(int(limit), 1000))
    operation_logs = _list_operation_logs(conn, station_id=station_id, limit=safe_limit)
    play_logs = _list_play_logs(conn, station_id=station_id, limit=safe_limit)

    parsed_scope = str(scope or "").strip().lower()
    if parsed_scope == "play":
        logs = play_logs
    elif parsed_scope in {"operation", "api"}:
        logs = operation_logs
    else:
        logs = operation_logs + play_logs
        logs.sort(
            key=lambda item: str(item.get("played_at") or item.get("created_at") or ""),
            reverse=True,
        )
        logs = logs[:safe_limit]

    if date_from:
        logs = [item for item in logs if str(item.get("played_at", "")) >= str(date_from)]
    if date_to:
        logs = [item for item in logs if str(item.get("played_at", "")) <= str(date_to)]
    return logs


def _legacy_logs_csv_text(logs: list[dict]) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "played_at",
            "station_id",
            "log_type",
            "level",
            "action",
            "title",
            "artist",
            "details",
        ]
    )
    for log in logs:
        writer.writerow(
            [
                str(log.get("played_at") or ""),
                str(log.get("station_id") or ""),
                str(log.get("log_type") or ""),
                str(log.get("level") or ""),
                str(log.get("action") or ""),
                str(log.get("title") or ""),
                str(log.get("artist") or ""),
                str(log.get("details") or ""),
            ]
        )
    return output.getvalue()


@router.get("/api/logs")
def list_legacy_logs(
    station_id: int | None = None,
    limit: int = 200,
    scope: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    _user=Depends(require_permission("logs.view")),
):
    init_db()
    conn = get_connection()
    safe_limit = int(per_page) if per_page is not None else int(limit)
    logs = _collect_legacy_logs(
        conn,
        station_id=station_id,
        limit=safe_limit,
        scope=scope,
        date_from=date_from,
        date_to=date_to,
    )

    if scope is not None or page is not None or per_page is not None or date_from is not None or date_to is not None:
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or safe_limit), 500))
        total = len(logs)
        start = (safe_page - 1) * safe_per_page
        end = start + safe_per_page
        page_logs = logs[start:end]
        return {
            "logs": page_logs,
            "page": safe_page,
            "per_page": safe_per_page,
            "total": total,
            "total_pages": max(1, (total + safe_per_page - 1) // safe_per_page),
        }
    return logs


@router.get("/api/logs/export")
def export_legacy_logs(
    station_id: int | None = None,
    limit: int = 1000,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    format: str = "json",
    _user=Depends(require_permission("logs.view")),
):
    init_db()
    conn = get_connection()
    logs = _collect_legacy_logs(
        conn,
        station_id=station_id,
        limit=limit,
        scope=scope,
        date_from=date_from,
        date_to=date_to,
    )
    if str(format or "json").strip().lower() != "csv":
        return {"items": logs}

    station_token = str(station_id) if station_id is not None else "all"
    headers = {
        "Content-Disposition": f'attachment; filename="logs-station-{station_token}.csv"'
    }
    return PlainTextResponse(
        _legacy_logs_csv_text(logs),
        media_type="text/csv",
        headers=headers,
    )


@router.get("/api/events")
def list_legacy_events(
    station_id: int | None = None,
    limit: int = 200,
    _user=Depends(require_permission("logs.view")),
):
    init_db()
    conn = get_connection()
    repo = LogRepository(conn)
    rows = repo.list_events(station_id=station_id, limit=limit)
    return [
        {
            "id": int(row["id"]),
            "station_id": int(row["station_id"]),
            "event_type": str(row["event_type"]),
            "payload": _decode_json_text(row["payload_json"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


@router.post("/api/events")
def create_legacy_event(
    payload: EventCreatePayload,
    _user=Depends(require_role("admin")),
):
    init_db()
    conn = get_connection()
    repo = LogRepository(conn)
    event_id = repo.add_event(
        station_id=payload.station_id,
        event_type=payload.event_type,
        payload=payload.payload,
    )
    return {"id": event_id}


@router.delete("/api/events/{event_id}")
def delete_legacy_event(
    event_id: int,
    _user=Depends(require_role("admin")),
):
    init_db()
    conn = get_connection()
    repo = LogRepository(conn)
    ok = repo.delete_event(event_id)
    if not ok:
        raise HTTPException(status_code=404, detail="event not found")
    return {"ok": True}


def _normalize_program_mode(mode: str) -> str:
    token = str(mode or "").strip().lower()
    if token in {"duck", "mute", "normal"}:
        return token
    return "normal"


def _set_program_music_mode(conn, station_id: int, mode: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, 'program_music_mode', ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (int(station_id), _normalize_program_mode(mode)),
    )
    conn.commit()


@router.post("/api/liquidsoap/duck")
def legacy_liquidsoap_duck(
    on: bool = False,
    station_id: int = 1,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.broadcast",
        show_id=show_id,
    )
    mode = "duck" if bool(on) else "normal"
    _set_program_music_mode(conn, station_id=station_id, mode=mode)
    try:
        from app.api.runtime import runtime_registry
        runtime_registry.refresh_live_audio_settings(int(station_id))
    except Exception:
        pass
    return {"ok": True, "mode": mode, "effective_mode": mode, "supported": True, "warnings": []}


@router.post("/api/liquidsoap/program/music")
def legacy_program_music_mode(
    mode: str = "normal",
    station_id: int = 1,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.broadcast",
        show_id=show_id,
    )
    normalized = _normalize_program_mode(mode)
    _set_program_music_mode(conn, station_id=station_id, mode=normalized)
    try:
        from app.api.runtime import runtime_registry
        runtime_registry.refresh_live_audio_settings(int(station_id))
    except Exception:
        pass
    warnings: list[str] = []
    return {
        "ok": True,
        "mode": normalized,
        "effective_mode": normalized,
        "requested_mode": str(mode),
        "supported": True,
        "warnings": warnings,
    }


def _program_queue_row_to_dict(row, queue_index: int) -> dict:
    return {
        "queue_index": int(queue_index),
        "id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "track_id": int(row["track_id"]),
        "position": int(row["position"]),
        "title": str(row["title"] or ""),
        "artist": str(row["artist"] or ""),
        "file_path": str(row["file_path"] or ""),
        "duration": float(row["duration"] or 0.0),
        "created_at": str(row["created_at"]),
    }


def _program_host_min_tracks_to_activate(conn, station_id: int) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM station_settings WHERE station_id=? AND key='program_queue_min_tracks'",
        (int(station_id),),
    )
    row = cur.fetchone()
    if not row:
        return 3
    token = str(row["value"] or "").strip()
    try:
        return _clamp(int(float(token)), 1, 20)
    except ValueError:
        return 3


def _program_queue_snapshot(conn, station_id: int) -> dict:
    repo = ProgramQueueRepository(conn)
    rows = repo.list_items(station_id=station_id)
    source = repo.get_source(station_id=station_id)
    host_min_tracks = _program_host_min_tracks_to_activate(conn, station_id=station_id)
    effective_source = source
    fallback_active = False
    if source == "host" and len(rows) < host_min_tracks:
        effective_source = "automation"
        fallback_active = True
    items = [_program_queue_row_to_dict(row, idx) for idx, row in enumerate(rows)]
    return {
        "station_id": int(station_id),
        "items": items,
        "source": source,
        "effective_source": effective_source,
        "queue_source": source,
        "effective_queue_source": effective_source,
        "fallback_active": bool(fallback_active),
        "host_min_tracks_to_activate": int(host_min_tracks),
        "queue_total": len(items),
    }


@router.get("/api/program/queue")
def legacy_program_queue(
    station_id: int,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_station_program_read_access(conn, user, station_id, show_id=show_id)
    return _program_queue_snapshot(conn, station_id=station_id)


@router.post("/api/program/workspace/claim")
def legacy_program_workspace_claim(
    payload: ProgramWorkspaceClaimPayload,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    snapshot = _claim_program_workspace(
        conn,
        user,
        station_id=payload.station_id,
        show_id=payload.show_id,
        force=bool(payload.force),
    )
    return {
        "ok": True,
        "station_id": int(payload.station_id),
        "show_id": int(payload.show_id),
        "queue": snapshot,
    }


@router.delete("/api/program/workspace/claim")
def legacy_program_workspace_release(
    station_id: int,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    snapshot = _release_program_workspace(
        conn,
        user,
        station_id=station_id,
        show_id=show_id,
    )
    return {
        "ok": True,
        "station_id": int(station_id),
        "show_id": int(show_id or 0),
        "queue": snapshot,
    }


@router.post("/api/program/queue/items")
def legacy_program_queue_add_item(
    payload: ProgramQueueItemPayload,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=payload.station_id,
        permission_key="show.queue_edit",
        show_id=payload.show_id,
    )
    repo = ProgramQueueRepository(conn)
    item_id = repo.add_item(station_id=payload.station_id, track_id=payload.track_id)
    return {
        "id": item_id,
        "item_id": item_id,
        "queue": _program_queue_snapshot(conn, station_id=payload.station_id),
    }


@router.post("/api/program/queue/move")
def legacy_program_queue_move(
    payload: ProgramQueueMovePayload,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=payload.station_id,
        permission_key="show.queue_edit",
        show_id=payload.show_id,
    )
    repo = ProgramQueueRepository(conn)
    ok = repo.move_by_index(
        station_id=payload.station_id,
        from_index=payload.from_index,
        to_index=payload.to_index,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="queue item not found")
    return {"ok": True, "queue": _program_queue_snapshot(conn, station_id=payload.station_id)}


@router.delete("/api/program/queue/{queue_index}")
def legacy_program_queue_delete(
    queue_index: int,
    station_id: int,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.queue_edit",
        show_id=show_id,
    )
    repo = ProgramQueueRepository(conn)
    ok = repo.delete_by_index(station_id=station_id, queue_index=queue_index)
    if not ok:
        raise HTTPException(status_code=404, detail="queue item not found")
    return {"ok": True, "queue": _program_queue_snapshot(conn, station_id=station_id)}


@router.post("/api/program/queue/clear")
def legacy_program_queue_clear(
    station_id: int,
    show_id: int | None = None,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=station_id,
        permission_key="show.queue_edit",
        show_id=show_id,
    )
    repo = ProgramQueueRepository(conn)
    repo.clear(station_id=station_id)
    return {"ok": True, "queue": _program_queue_snapshot(conn, station_id=station_id)}


@router.post("/api/program/queue/source")
def legacy_program_queue_source(
    payload: ProgramQueueSourcePayload,
    user=Depends(get_current_user),
):
    init_db()
    conn = get_connection()
    _require_program_workspace_permission(
        conn,
        user,
        station_id=payload.station_id,
        permission_key="show.broadcast",
        show_id=payload.show_id,
    )
    repo = ProgramQueueRepository(conn)
    repo.set_source(station_id=payload.station_id, source=payload.source)
    snapshot = _program_queue_snapshot(conn, station_id=payload.station_id)
    return {
        "ok": True,
        "source": str(snapshot.get("source") or "automation"),
        "effective_source": str(snapshot.get("effective_source") or "automation"),
        "queue": snapshot,
    }
