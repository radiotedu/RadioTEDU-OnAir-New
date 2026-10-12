from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.db import get_connection, init_db
from app.runtime_paths import get_data_dir
from app.services.special_events import (
    AUDIO_EXTENSIONS, MAX_TRACKS, active_programs, active_window,
    delete_program, read_programs, save_program, scan_folder,
)

router = APIRouter()


class EventTrack(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    title: str = Field(default='', max_length=256)
    artist: str = Field(default='', max_length=256)
    duration: float = Field(default=0, ge=0, le=86400)


class EventPayload(BaseModel):
    client_id: str = Field(default='', max_length=36, pattern=r'^[a-fA-F0-9-]*$')
    name: str = Field(min_length=1, max_length=120)
    mode: Literal['playlist', 'metadata'] = 'playlist'
    starts_on: date
    ends_on: date
    local_start: str = '00:00'
    local_end: str = '00:00'
    timezone: str = 'Europe/Istanbul'
    annual: bool = False
    enabled: bool = True
    priority: int = Field(default=100, ge=-100, le=1000)
    metadata_title: str = Field(default='', max_length=256)
    source_folder: str = Field(default='', max_length=4096)
    recursive: bool = True
    tracks: list[EventTrack] = Field(default_factory=list, max_length=MAX_TRACKS)
    order: Literal['ordered', 'shuffle'] = 'ordered'
    repeat_playlist: bool = True
    start_policy: Literal['exact', 'boundary'] = 'exact'
    crossfade_seconds: float = Field(default=3, ge=0, le=10)


class FolderPayload(BaseModel):
    folder: str = Field(min_length=1, max_length=4096)
    recursive: bool = True


def _station(conn, station_id):
    if not conn.execute('SELECT 1 FROM stations WHERE id=?', (station_id,)).fetchone():
        raise HTTPException(404, 'station_not_found')


def _validated(payload):
    if not payload.name.strip():
        raise ValueError('programme_name_required')
    if payload.ends_on < payload.starts_on:
        raise ValueError('end_date_before_start_date')
    ZoneInfo(payload.timezone)
    for text in (payload.local_start, payload.local_end):
        parsed = datetime.strptime(text, '%H:%M')
        if parsed.strftime('%H:%M') != text:
            raise ValueError('use_HH_MM_time')
    if any(ord(c) < 32 for c in payload.metadata_title):
        raise ValueError('metadata_must_be_one_line')
    if payload.mode == 'metadata' and not payload.metadata_title.strip():
        raise ValueError('fixed_metadata_text_required')
    if payload.mode == 'playlist' and not payload.tracks:
        raise ValueError('select_at_least_one_song')
    folder = Path(payload.source_folder).resolve() if payload.source_folder else None
    uploaded_root = (get_data_dir() / 'special-event-media').resolve()
    seen = set()
    for track in payload.tracks:
        p = Path(track.path)
        if not p.is_absolute() or p.is_symlink() or (payload.enabled and not p.is_file()) or p.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError('selected_audio_file_unavailable')
        p = p.resolve()
        if folder and p != folder and folder not in p.parents:
            raise ValueError('selected_audio_not_in_source_folder')
        if not folder and uploaded_root not in p.parents:
            raise ValueError('choose_a_source_folder_or_import_audio')
        identity = os.path.normcase(str(p))
        if identity in seen:
            raise ValueError('duplicate_song_in_programme')
        seen.add(identity)
        track.path = str(p)
    data = payload.model_dump(mode='json')
    data['name'] = payload.name.strip()
    data['metadata_title'] = payload.metadata_title.strip()
    return data


def _with_status(plan):
    window = active_window(plan, datetime.now(timezone.utc))
    return {**plan, 'active_now': window is not None,
            'active_until': window['end'].isoformat() if window else None,
            'track_count': len(plan.get('tracks', []))}


@router.get('/api/stations/{station_id}/special-events')
def list_events(station_id: int):
    init_db()
    conn = get_connection(timeout_seconds=3)
    try:
        _station(conn, station_id)
        plans = read_programs(conn, station_id)
        audio, metadata = active_programs(plans)
        return {'programs': [_with_status(p) for p in plans],
                'active_program_id': audio[0]['id'] if audio else None,
                'effective_metadata': metadata}
    finally:
        conn.close()


@router.post('/api/stations/{station_id}/special-events')
def create_event(station_id: int, payload: EventPayload):
    return _save(station_id, payload)


@router.put('/api/stations/{station_id}/special-events/{program_id}')
def update_event(station_id: int, program_id: str, payload: EventPayload):
    return _save(station_id, payload, program_id)


def _save(station_id, payload, program_id=None):
    init_db()
    conn = get_connection()
    try:
        _station(conn, station_id)
        data = _validated(payload)
        program = save_program(conn, station_id, data, program_id)
        return {'ok': True, 'program': _with_status(program)}
    except sqlite3.OperationalError:
        conn.rollback()
        raise HTTPException(409, 'special_event_storage_busy_retry') from None
    except KeyError:
        raise HTTPException(404, 'special_event_not_found') from None
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from None
    finally:
        conn.close()


@router.delete('/api/stations/{station_id}/special-events/{program_id}')
def remove_event(station_id: int, program_id: str):
    init_db()
    conn = get_connection()
    try:
        _station(conn, station_id)
        delete_program(conn, station_id, program_id)
        return {'ok': True}
    except KeyError:
        raise HTTPException(404, 'special_event_not_found') from None
    finally:
        conn.close()


@router.post('/api/special-event-media/preview-folder')
def preview_folder(payload: FolderPayload):
    try:
        tracks = scan_folder(payload.folder, payload.recursive)
        return {'ok': True, 'tracks': tracks, 'count': len(tracks), 'complete': True}
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/api/special-event-media/upload')
async def upload_event_audio(file: UploadFile = File(...)):
    # The browser folder chooser provides bytes, never an absolute local path.
    # Store each selected song atomically in an independent, durable media pool.
    name = Path(str(file.filename or '').replace('\\', '/')).name
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        raise HTTPException(422, 'unsupported_audio_file')
    root = get_data_dir() / 'special-event-media'
    root.mkdir(parents=True, exist_ok=True)
    import shutil
    if shutil.disk_usage(root).free < 1024 * 1024 * 1024:
        raise HTTPException(507, 'insufficient_storage_for_audio_import')
    target = root / (uuid.uuid4().hex + suffix)
    temporary = target.with_suffix(suffix + '.part')
    size = 0
    try:
        with temporary.open('xb') as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise HTTPException(413, 'audio_file_limit_512_MB')
                await run_in_threadpool(handle.write, chunk)
            await run_in_threadpool(handle.flush)
            await run_in_threadpool(os.fsync, handle.fileno())
        if not size:
            raise HTTPException(422, 'empty_audio_file')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()
    return {'ok': True, 'track': {'path': str(target.resolve()), 'title': Path(name).stem,
                                 'artist': '', 'duration': 0.0}}
