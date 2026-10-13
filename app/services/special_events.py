"""Durable, station-local special-day programmes; never changes output settings."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_db_path
from app.repositories.settings_repo import SettingsRepository

CONFIG_KEY = 'special_event_programs_v1'
STATE_KEY = 'special_event_playout_v1'
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.wav', '.ogg', '.opus', '.m4a', '.aac', '.aiff', '.aif', '.wma'}
MAX_TRACKS = 2000
_log = logging.getLogger(__name__)
_cache = {}


def ensure_schema(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS special_event_programs ("
                "station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE, "
                "id TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY(station_id,id))")
    cur.execute("CREATE TABLE IF NOT EXISTS special_event_playout ("
                "station_id INTEGER PRIMARY KEY REFERENCES stations(id) ON DELETE CASCADE, "
                "state_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")


def read_programs(conn, station_id):
    rows = conn.execute('SELECT payload_json FROM special_event_programs WHERE station_id=? ORDER BY id',
                        (int(station_id),)).fetchall()
    return [json.loads(row[0]) for row in rows]


def save_program(conn, station_id, values, program_id=None):
    # Server-owned identity/revision are not part of the editable payload.
    values = {k: v for k, v in values.items() if k not in {'id', 'revision'}}
    # Serialize read/modify/write to avoid losing concurrent operator edits.
    conn.execute('BEGIN IMMEDIATE')
    try:
        plans = read_programs(conn, station_id)
        if program_id and not any(p['id'] == program_id for p in plans):
            raise KeyError('special_event_not_found')
        saved_id = program_id or values.get('client_id') or uuid.uuid4().hex
        existing = next((p for p in plans if p['id'] == saved_id), None)
        if existing and all(existing.get(k) == v for k, v in values.items()):
            conn.rollback()
            return existing
        if existing and not program_id:
            raise ValueError('creation_token_already_used_for_a_different_plan')
        program = dict(values, id=saved_id, revision=uuid.uuid4().hex)
        plans = [p for p in plans if p['id'] != program['id']] + [program]
        if len(plans) > 64:
            raise ValueError('maximum_64_special_programmes_per_station')
        conn.execute("INSERT INTO special_event_programs(station_id,id,payload_json) VALUES (?,?,?) "
                     "ON CONFLICT(station_id,id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP",
                     (int(station_id), program['id'], json.dumps(program, ensure_ascii=False)))
        conn.commit()
        _cache.clear()
        return program
    except Exception:
        conn.rollback()
        raise


def delete_program(conn, station_id, program_id):
    conn.execute('BEGIN IMMEDIATE')
    try:
        plans = read_programs(conn, station_id)
        remaining = [p for p in plans if p['id'] != program_id]
        if len(remaining) == len(plans):
            raise KeyError('special_event_not_found')
        conn.execute('DELETE FROM special_event_programs WHERE station_id=? AND id=?',
                     (int(station_id), program_id))
        conn.commit()
        _cache.clear()
    except Exception:
        conn.rollback()
        raise


def scan_folder(folder, recursive=True):
    root = Path(folder).expanduser()
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('select_an_existing_absolute_folder')
    root = root.resolve()
    paths, errors = [], []
    def failed(error):
        errors.append(str(error))
    for directory, dirs, names in os.walk(root, onerror=failed, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink()
                         and d not in {'.git', '.trash', '.radiotedu-trash'}) if recursive else []
        for name in sorted(names):
            p = Path(directory) / name
            if p.suffix.lower() in AUDIO_EXTENSIONS and not p.is_symlink():
                if not p.is_file():
                    errors.append('file_changed_during_scan')
                    continue
                paths.append(p)
                if len(paths) > MAX_TRACKS:
                    raise ValueError('folder_exceeds_2000_audio_files_select_a_smaller_folder')
    if errors:
        raise ValueError('folder_scan_incomplete_previous_list_preserved')
    return [{'path': str(p.resolve()), 'title': p.stem, 'artist': '', 'duration': 0.0,
             'relative_path': str(p.relative_to(root))} for p in paths]


def active_window(plan, now):
    if not plan.get('enabled', True):
        return None
    zone = ZoneInfo(plan.get('timezone', 'Europe/Istanbul'))
    local = now.astimezone(zone)
    starts, ends = date.fromisoformat(plan['starts_on']), date.fromisoformat(plan['ends_on'])
    start_clock = datetime.strptime(plan['local_start'], '%H:%M').time()
    end_clock = datetime.strptime(plan['local_end'], '%H:%M').time()
    for anchor in (local.date(), local.date() - timedelta(days=1)):
        if plan.get('annual'):
            a, b, d = (starts.month, starts.day), (ends.month, ends.day), (anchor.month, anchor.day)
            eligible = a <= d <= b if a <= b else d >= a or d <= b
        else:
            eligible = starts <= anchor <= ends
        if not eligible:
            continue
        start = datetime.combine(anchor, start_clock, tzinfo=zone)
        end = datetime.combine(anchor, end_clock, tzinfo=zone)
        if end <= start:
            end += timedelta(days=1)
        # Compare instants, not wall clocks, across DST folds.
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if start <= now < end:
            return {'start': start, 'end': end,
                    'key': plan['id'] + ':' + start.isoformat()}
    return None


def active_programs(plans, now=None):
    now = now or datetime.now(timezone.utc)
    candidates = [(p, active_window(p, now)) for p in plans]
    candidates = [(p, w) for p, w in candidates if w]
    candidates.sort(key=lambda pair: (-int(pair[0].get('priority', 0)), pair[0]['id']))
    audio = next(((p, w) for p, w in candidates if p.get('mode', 'playlist') == 'playlist'), None)
    metadata = next((p.get('metadata_title', '') for p, w in candidates if p.get('metadata_title')), None)
    return audio, metadata


def _load_cached(conn, station_id):
    key = (str(get_db_path()), int(station_id))
    now = time.monotonic()
    previous = _cache.get(key)
    if previous and now - previous[0] < 1:
        return previous[1]
    plans = read_programs(conn, station_id)
    _cache[key] = (now, plans)
    return plans


def _read_state(conn, station_id):
    row = conn.execute('SELECT state_json FROM special_event_playout WHERE station_id=?',
                       (station_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def _write_state(conn, station_id, state):
    conn.execute("INSERT INTO special_event_playout(station_id,state_json) VALUES (?,?) "
                 "ON CONFLICT(station_id) DO UPDATE SET state_json=excluded.state_json,updated_at=CURRENT_TIMESTAMP",
                 (int(station_id), json.dumps(state, ensure_ascii=False)))
    conn.commit()


def _same_media(left, right):
    from urllib.parse import unquote, urlsplit
    def normalize(value):
        token = str(value or '').replace('\\', '/')
        if token.lower().startswith('file://'):
            parts = urlsplit(token)
            token = unquote(parts.path)
            if parts.netloc and parts.netloc.lower() != 'localhost':
                token = '//' + parts.netloc + token
            if os.name == 'nt' and len(token) > 2 and token[0] == '/' and token[2] == ':':
                token = token[1:]
        return os.path.normcase(os.path.abspath(token)) if token else ''
    return bool(left) and bool(right) and normalize(left) == normalize(right)


def _ordered_tracks(plan, window, cycle):
    tracks = list(plan['tracks'])
    if plan.get('order') == 'shuffle':
        seed = hashlib.sha256((window['key'] + ':' + str(cycle)).encode()).hexdigest()
        random.Random(seed).shuffle(tracks)
    return tracks


def refresh_special_metadata(worker, now=None):
    if not worker.runtime_registry:
        return
    plans = _load_cached(worker.conn, worker.station_id)
    _, title = active_programs(plans, now)
    setter = getattr(worker.runtime_registry, 'set_program_metadata_override', None)
    if callable(setter):
        setter(worker.station_id, title)


def process_special_event(worker, now=None):
    """Own only the selected station's music, keeping its encoders and queue."""
    if not worker.runtime_registry:
        return None
    now = now or datetime.now(timezone.utc)
    plans = _load_cached(worker.conn, worker.station_id)
    audio, metadata = active_programs(plans, now)
    set_metadata = getattr(worker.runtime_registry, 'set_program_metadata_override', None)
    if callable(set_metadata):
        set_metadata(worker.station_id, metadata)
    state = _read_state(worker.conn, worker.station_id) if plans or metadata else {}
    if not audio:
        if state:
            _write_state(worker.conn, worker.station_id, {})
        return None
    plan, window = audio
    same = state.get('occurrence') == window['key'] and state.get('revision') == plan['revision']
    runtime = worker.runtime_registry.status(worker.station_id) or {}
    running = worker._runtime_playback_alive(runtime)
    if not same:
        if plan.get('start_policy', 'boundary') == 'boundary':
            # A previously-owned special programme is also allowed to reach EOF.
            uri = runtime.get('active_input_uri', '')
            if (running or worker._runtime_source_is_draining(runtime, uri)) and not worker._runtime_source_finished_naturally(runtime, uri):
                return {'source': 'playing', 'reason': 'special_event_waiting_for_boundary'}
        state = {'occurrence': window['key'], 'revision': plan['revision'], 'index': 0,
                 'cycle': 0, 'uri': '', 'started_epoch': 0, 'failed_paths': []}
    if state.get('completed'):
        return None
    tracks = _ordered_tracks(plan, window, state['cycle'])
    if not tracks:
        return None
    if state.get('uri') and _same_media(state['uri'], runtime.get('active_input_uri', '')):
        finished = worker._runtime_source_finished_naturally(runtime, runtime.get('active_input_uri', ''))
        draining = worker._runtime_source_is_draining(runtime, state['uri'])
        if (running and not finished) or (draining and not finished):
            if draining:
                prepare = getattr(worker.runtime_registry, 'prepare_next_station_source', None)
                next_index, next_cycle = state['index'] + 1, state['cycle']
                if next_index >= len(tracks) and plan.get('repeat_playlist', True):
                    next_index, next_cycle = 0, next_cycle + 1
                future = _ordered_tracks(plan, window, next_cycle)
                if callable(prepare) and next_index < len(future):
                    track = future[next_index]
                    try:
                        prepare(worker.station_id, track['path'], stream_title=track.get('title', ''),
                                stream_artist=track.get('artist', ''), track_type='music')
                    except Exception:
                        _log.debug('Special playlist preparation deferred', exc_info=True)
            return {'source': 'special_event', 'program_id': plan['id'], 'reason': 'track_in_progress'}
        if finished:
            state['index'] += 1
            state['uri'] = ''
        elif now.timestamp() - float(state.get('requested_at', state.get('started_epoch', 0))) < 10 and runtime.get('producer_exit_code') is None:
            return {'source': 'special_event', 'program_id': plan['id'], 'reason': 'source_starting'}
        else:
            # A corrupt file or stalled decoder must not be restarted every poll.
            state.setdefault('failed_paths', []).append(state['uri'])
            state['index'] += 1
            state['uri'] = ''
    retry_after = float(state.get('retry_after', 0))
    if retry_after > now.timestamp():
        return None  # Ordinary continuity fills a missing/unavailable pool.
    # Limit failed-file work per poll; a missing large folder must not starve continuity.
    for _ in range(min(8, len(tracks))):
        if state['index'] >= len(tracks):
            if not plan.get('repeat_playlist', True):
                state['completed'] = True
                _write_state(worker.conn, worker.station_id, state)
                return None
            state['cycle'] += 1
            state['index'] = 0
            tracks = _ordered_tracks(plan, window, state['cycle'])
        track = tracks[state['index']]
        file = Path(track['path'])
        if str(file) in state.get('failed_paths', []):
            state['index'] += 1
            continue
        try:
            if not file.is_file() or file.is_symlink():
                raise OSError('special_event_audio_missing')
            resume = 0.0
            if state.get('uri') == str(file):
                resume = max(0, now.timestamp() - float(state.get('started_epoch', now.timestamp())))
                duration = float(track.get('duration', 0) or 0)
                if duration > 0:
                    resume = min(resume, max(0, duration - 1))
            restart = {}
            if (runtime.get('producer_eof') and _same_media(str(file), runtime.get('active_input_uri', ''))
                    and worker.runtime_registry.is_process_running(worker.station_id)):
                restart = {'force_restart': True, 'expected_active_input_uri': str(file)}
            if not same:
                # Complete only rows whose own media has proven EOF, before
                # the registry changes URI. Keep the normal ledger callbacks.
                worker._complete_special_event_predecessor(runtime)
            first_boundary = not same and plan.get('start_policy', 'boundary') == 'boundary'
            worker._start_runtime_station(worker.station_id, str(file),
                stream_title=track.get('title') or file.stem, stream_artist=track.get('artist', ''),
                track_type='music', crossfade_seconds=0.0 if first_boundary else float(plan.get('crossfade_seconds', 3)),
                start_offset_seconds=resume, **restart)
            # Requeue, rather than consume, interrupted regular automation items.
            # Their content is not edited or deleted and resumes when the window ends.
            for table in ('queue_items', 'ad_break_items', 'schedule_items'):
                worker.conn.execute("UPDATE " + table + " SET status='pending', started_at=NULL "
                                    "WHERE station_id=? AND status='playing'" if table != 'schedule_items'
                                    else "UPDATE schedule_items SET status='pending' WHERE station_id=? AND status='playing'",
                                    (worker.station_id,))
            worker.conn.commit()
            state.update(uri=str(file), started_epoch=now.timestamp() - resume, requested_at=now.timestamp(), retry_after=0, last_error='')
            _write_state(worker.conn, worker.station_id, state)
            worker._set_playout_state('special_event', None, reason='special_event_programme')
            return {'source': 'special_event', 'program_id': plan['id'], 'title': plan['name']}
        except (OSError, ValueError, RuntimeError):
            _log.exception('Special programme source unavailable station_id=%s', worker.station_id)
            state.setdefault('failed_paths', []).append(str(file))
            state['index'] += 1
    state.update(uri='', retry_after=now.timestamp() + 30, failed_paths=[], last_error='audio_unavailable_continuity_active')
    _write_state(worker.conn, worker.station_id, state)
    return None
