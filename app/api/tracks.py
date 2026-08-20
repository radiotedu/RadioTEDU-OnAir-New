from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.audio.audio_processing import (
    clean_intro as _clean_intro,
    process_imported_track,
    trim_manual as _trim_manual_file,
    trim_silence as _trim_silence_file,
)
from app.db import get_connection, init_db
from app.metadata.picard import normalize_metadata
from app.repositories.track_repo import TrackRepository
from app.services.music_usage import MusicUsageService

router = APIRouter()


class TrackUpsertPayload(BaseModel):
    station_id: int = 1
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    language: str = ""
    duration: float = 0.0
    bpm: float = 0.0
    track_type: str = "music"
    file_path: str = ""
    musicbrainz_recordingid: str = ""
    version: str = ""
    composer: str = ""
    lyricist: str = ""
    phonogram_producer: str = ""
    label: str = ""
    isrc: str = ""
    source_reference: str = ""
    rights_reference: str = ""
    source_type: str = ""


class TrackUpdatePayload(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    language: str | None = None
    track_type: str | None = None
    exclude_from_autoplay: bool | None = None


class TrimManualPayload(BaseModel):
    start_cut: float = 0.0
    end_cut: float = 0.0


class TrimSilencePayload(BaseModel):
    threshold_db: float = -45.0
    min_silence: float = 0.15
    skip_if_already_trimmed: bool = False


class CensorSegment(BaseModel):
    start: float
    end: float
    effect: str = "beep"


class CensorPayload(BaseModel):
    segments: list[CensorSegment] = Field(default_factory=list)


class TrimBatchPayload(BaseModel):
    station_id: int = 1
    track_type: str = "any"
    only_untrimmed: bool = True
    only_imports: bool = False
    limit: int = 500


def _sanitize_track_type(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if token in {"ad", "ads"}:
        return "ad"
    if token in {"music", "jingle", "startup"}:
        return token
    return "music"


def _row_to_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "station_id": int(row["station_id"] or 1),
        "title": str(row["title"] or ""),
        "artist": str(row["artist"] or ""),
        "album": str(row["album"] or ""),
        "genre": str(row["genre"] or ""),
        "language": str(row["language"] or ""),
        "duration": float(row["duration"] or 0.0),
        "bpm": float(row["bpm"] or 0.0),
        "track_type": str(row["track_type"] or "music"),
        "file_path": str(row["file_path"] or ""),
        "musicbrainz_recordingid": str(row["musicbrainz_recordingid"] or ""),
        "is_active": bool(int(row["is_active"] or 0)),
        "exclude_from_autoplay": bool(int(row["exclude_from_autoplay"] or 0)),
        "play_count": int(row["play_count"] or 0),
        "last_played_at": str(row["last_played_at"] or ""),
    }


@router.post("/api/tracks")
def upsert_track(payload: TrackUpsertPayload):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    normalized = normalize_metadata(payload.model_dump())
    track_id = repo.upsert(
        station_id=int(normalized.get("station_id", payload.station_id or 1)),
        title=str(normalized.get("title", "")),
        artist=str(normalized.get("artist", "")),
        album=str(normalized.get("album", "")),
        genre=str(normalized.get("genre", "")),
        language=str(normalized.get("language", "")),
        duration=float(normalized.get("duration", payload.duration or 0.0)),
        bpm=float(normalized.get("bpm", payload.bpm or 0.0)),
        track_type=_sanitize_track_type(str(normalized.get("track_type", payload.track_type))),
        file_path=str(normalized.get("file_path", "")),
        musicbrainz_recordingid=str(normalized.get("musicbrainz_recordingid", "")),
    )
    if any(
        str(getattr(payload, field, "") or "").strip()
        for field in (
            "version",
            "composer",
            "lyricist",
            "phonogram_producer",
            "label",
            "isrc",
            "source_reference",
            "rights_reference",
            "source_type",
        )
    ):
        MusicUsageService(conn).upsert_track_metadata(track_id, payload.model_dump())
    return {"ok": True, "track_id": track_id}


@router.get("/api/tracks")
def list_tracks(
    station_id: int = 1,
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    q: str = "",
    limit: int | None = None,
    artist: str = "",
    genre: str = "",
    language: str = "",
    track_type: str = "",
    library_scope: str = "local",
    source_station_id: int | None = None,
):
    init_db()
    conn = get_connection()

    safe_page = max(1, int(page))
    if limit is not None:
        safe_per_page = max(1, min(int(limit), 500))
        safe_page = 1
    else:
        safe_per_page = max(1, min(int(per_page), 500))
    offset = (safe_page - 1) * safe_per_page

    scope = str(library_scope or "local").strip().lower()
    text = str(search or q or "").strip()
    artist_token = str(artist or "").strip()
    genre_token = str(genre or "").strip()
    lang_token = str(language or "").strip()
    track_type_token = str(track_type or "").strip().lower()

    where_parts = ["t.is_active=1"]
    params: list = []

    if scope == "station" and source_station_id is not None:
        where_parts.append("t.station_id=?")
        params.append(int(source_station_id))
    elif scope == "local":
        where_parts.append("t.station_id=?")
        params.append(int(station_id))

    if track_type_token and track_type_token != "any":
        where_parts.append("LOWER(t.track_type)=?")
        params.append(track_type_token)
    else:
        # Hide startup sounds from normal library listing
        where_parts.append("LOWER(COALESCE(t.track_type,'music'))!='startup'")
    if artist_token:
        where_parts.append("LOWER(t.artist)=LOWER(?)")
        params.append(artist_token)
    if genre_token:
        where_parts.append("LOWER(t.genre)=LOWER(?)")
        params.append(genre_token)
    if lang_token:
        where_parts.append("LOWER(t.language)=LOWER(?)")
        params.append(lang_token)
    if text:
        like = f"%{text}%"
        where_parts.append(
            "(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ? OR t.genre LIKE ? OR t.language LIKE ? OR t.file_path LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])

    where_sql = " AND ".join(where_parts)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS c FROM tracks t WHERE {where_sql}", tuple(params))
    total = int(cur.fetchone()["c"])
    total_pages = max(1, (total + safe_per_page - 1) // safe_per_page)

    cur.execute(
        f"SELECT t.id, t.station_id, t.title, t.artist, t.album, t.genre, t.language, t.duration, t.bpm, "
        "t.track_type, t.file_path, t.musicbrainz_recordingid, t.is_active, "
        "t.exclude_from_autoplay, t.play_count, t.last_played_at "
        f"FROM tracks t WHERE {where_sql} "
        "ORDER BY t.id DESC LIMIT ? OFFSET ?",
        tuple(params + [safe_per_page, offset]),
    )
    rows = cur.fetchall()
    tracks = [_row_to_payload(row) for row in rows]
    return {
        "items": tracks,
        "tracks": tracks,
        "page": safe_page,
        "per_page": safe_per_page,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/api/tracks/filters/options")
def track_filter_options(
    station_id: int = 1,
    library_scope: str = "local",
    source_station_id: int | None = None,
):
    init_db()
    conn = get_connection()
    scope = str(library_scope or "local").strip().lower()
    params: list = []
    where = "is_active=1"
    if scope == "station" and source_station_id is not None:
        where += " AND station_id=?"
        params.append(int(source_station_id))
    elif scope == "local":
        where += " AND station_id=?"
        params.append(int(station_id))

    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT artist FROM tracks WHERE {where} AND TRIM(COALESCE(artist,'')) <> '' ORDER BY artist COLLATE NOCASE ASC",
        tuple(params),
    )
    artists = [str(row["artist"]) for row in cur.fetchall()]
    cur.execute(
        f"SELECT DISTINCT genre FROM tracks WHERE {where} AND TRIM(COALESCE(genre,'')) <> '' ORDER BY genre COLLATE NOCASE ASC",
        tuple(params),
    )
    genres = [str(row["genre"]) for row in cur.fetchall()]
    cur.execute(
        f"SELECT DISTINCT language FROM tracks WHERE {where} AND TRIM(COALESCE(language,'')) <> '' ORDER BY language COLLATE NOCASE ASC",
        tuple(params),
    )
    languages = [str(row["language"]) for row in cur.fetchall()]
    return {"artists": artists, "genres": genres, "languages": languages}


@router.get("/api/tracks/next")
def get_next_track(station_id: int = 1):
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT t.id, t.station_id, t.title, t.artist, t.album, t.genre, t.language, t.duration, t.bpm, "
        "t.track_type, t.file_path, t.musicbrainz_recordingid, t.is_active, "
        "t.exclude_from_autoplay, t.play_count, t.last_played_at "
        "FROM queue_items q "
        "JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=? AND q.status IN ('playing','pending') "
        "ORDER BY CASE WHEN q.status='playing' THEN 0 ELSE 1 END, q.position ASC, q.id ASC LIMIT 1",
        (int(station_id),),
    )
    row = cur.fetchone()
    if not row:
        return {}
    return _row_to_payload(row)


@router.post("/api/tracks/trim/silence/batch")
def trim_silence_batch(payload: TrimBatchPayload):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    cur = conn.cursor()

    where_parts = ["is_active=1", "COALESCE(file_path, '') <> ''"]
    params: list = []

    where_parts.append("station_id=?")
    params.append(int(payload.station_id))

    track_type_token = str(payload.track_type or "any").strip().lower()
    if track_type_token and track_type_token != "any":
        where_parts.append("LOWER(track_type)=?")
        params.append(track_type_token)

    where_sql = " AND ".join(where_parts)
    cur.execute(
        f"SELECT id, file_path, duration FROM tracks WHERE {where_sql} "
        f"ORDER BY id ASC LIMIT ?",
        tuple(params + [max(1, int(payload.limit))]),
    )
    rows = cur.fetchall()

    trimmed = 0
    failed = 0
    skipped = 0
    removed_total = 0.0
    analyzed = 0

    for row in rows:
        file_path = str(row["file_path"] or "").strip()
        if not file_path:
            skipped += 1
            continue
        analyzed += 1
        try:
            result = _trim_silence_file(file_path)
            if result.get("trimmed"):
                trimmed += 1
                removed_total += float(result.get("removed_seconds", 0.0))
                new_dur = float(result.get("new_duration", 0.0))
                if new_dur > 0:
                    cur.execute(
                        "UPDATE tracks SET duration=? WHERE id=?",
                        (new_dur, int(row["id"])),
                    )
            else:
                skipped += 1
        except Exception:
            failed += 1

    conn.commit()
    return {
        "ok": True,
        "summary": {
            "trimmed": trimmed,
            "failed": failed,
            "removed_seconds_total": round(removed_total, 3),
            "analyzed": analyzed,
            "skipped": skipped,
        },
        "station_id": int(payload.station_id),
        "track_type": str(payload.track_type or "any"),
    }


@router.post("/api/tracks/{track_id}/trim/manual")
def trim_manual(track_id: int, payload: TrimManualPayload):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    row = repo.get(track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="track not found")

    file_path = str(row["file_path"] or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="track has no file_path")

    result = _trim_manual_file(
        file_path,
        start_cut=float(payload.start_cut),
        end_cut=float(payload.end_cut),
    )

    if result.get("trimmed"):
        new_dur = float(result.get("new_duration", 0.0))
        if new_dur > 0:
            cur = conn.cursor()
            cur.execute("UPDATE tracks SET duration=? WHERE id=?", (new_dur, track_id))
            conn.commit()

    return {
        "ok": True,
        "track_id": int(track_id),
        "start_cut": float(payload.start_cut),
        "end_cut": float(payload.end_cut),
        **result,
    }


@router.post("/api/tracks/{track_id}/trim/silence")
def trim_silence(track_id: int, payload: TrimSilencePayload):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    row = repo.get(track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="track not found")

    file_path = str(row["file_path"] or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="track has no file_path")

    result = _trim_silence_file(
        file_path,
        threshold_db=float(payload.threshold_db),
        min_silence=float(payload.min_silence),
    )

    if result.get("trimmed"):
        new_dur = float(result.get("new_duration", 0.0))
        if new_dur > 0:
            cur = conn.cursor()
            cur.execute("UPDATE tracks SET duration=? WHERE id=?", (new_dur, track_id))
            conn.commit()

    return {
        "ok": True,
        "track_id": int(track_id),
        "threshold_db": float(payload.threshold_db),
        "min_silence": float(payload.min_silence),
        **result,
    }


@router.post("/api/tracks/{track_id}/censor")
def censor_track(track_id: int, payload: CensorPayload):
    return {
        "ok": True,
        "track_id": int(track_id),
        "segments_applied": len(payload.segments or []),
    }


@router.get("/api/tracks/{track_id}")
def get_track(track_id: int):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    row = repo.get(track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="track not found")
    return _row_to_payload(row)


@router.put("/api/tracks/{track_id}")
def update_track(track_id: int, payload: TrackUpdatePayload):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    row = repo.get(track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="track not found")

    current = _row_to_payload(row)
    merged = {
        "title": current["title"] if payload.title is None else payload.title,
        "artist": current["artist"] if payload.artist is None else payload.artist,
        "album": current["album"] if payload.album is None else payload.album,
        "genre": current["genre"] if payload.genre is None else payload.genre,
        "language": current["language"] if payload.language is None else payload.language,
        "track_type": current["track_type"] if payload.track_type is None else payload.track_type,
    }
    normalized = normalize_metadata(merged)
    ok = repo.update(
        track_id=track_id,
        title=str(normalized.get("title", current["title"])),
        artist=str(normalized.get("artist", current["artist"])),
        album=str(normalized.get("album", current["album"])),
        genre=str(normalized.get("genre", current["genre"])),
        language=str(normalized.get("language", current["language"])),
        track_type=_sanitize_track_type(str(normalized.get("track_type", current["track_type"]))),
        exclude_from_autoplay=(
            current["exclude_from_autoplay"]
            if payload.exclude_from_autoplay is None
            else bool(payload.exclude_from_autoplay)
        ),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="track not found")
    return {"ok": True, "track_id": int(track_id)}


@router.delete("/api/tracks/{track_id}")
def delete_track(track_id: int):
    init_db()
    conn = get_connection()
    repo = TrackRepository(conn)
    outcome = repo.deactivate(track_id)
    if not bool(outcome.get("found")):
        raise HTTPException(status_code=404, detail="track not found")

    # Only stop runtimes where this deleted track was actively playing.
    if bool(outcome.get("deleted")):
        try:
            from app.api.runtime import runtime_registry

            for station_id in list(outcome.get("runtime_stop_stations") or []):
                runtime_registry.stop_station(int(station_id))
        except Exception:
            pass

    return {
        "ok": True,
        "track_id": int(track_id),
        **outcome,
    }
