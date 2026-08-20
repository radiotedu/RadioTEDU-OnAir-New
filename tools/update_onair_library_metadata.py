from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


os.environ.setdefault("CLEANROOM_DB_PATH", r"C:\ProgramData\RadioTEDU\OnAir\cleanroom.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import legacy  # noqa: E402
from app.db import get_connection, init_db  # noqa: E402
from app.file_security import audio_upload_extensions  # noqa: E402


INTERVAL_SECONDS = 600
LOCK_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "RadioTEDU" / "OnAir" / "metadata-refresh.lock"
STATE_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "RadioTEDU" / "OnAir" / "metadata-refresh-state.json"


def canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    os.write(handle, str(os.getpid()).encode("ascii"))
    os.close(handle)
    return True


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def load_state() -> dict[str, dict[str, int]]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_state(state: dict[str, dict[str, int]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATE_PATH)


def scan_profile(folder: str, state: dict[str, dict[str, int]]) -> tuple[list[tuple[Path, int]], dict[str, dict[str, int]], int]:
    root = Path(folder).expanduser()
    extensions = audio_upload_extensions()
    changed: list[tuple[Path, int]] = []
    next_state: dict[str, dict[str, int]] = {}
    scanned = 0
    if not root.exists() or not root.is_dir():
        return changed, next_state, scanned
    for path in root.rglob("*"):
        try:
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            stat = path.stat()
        except OSError:
            continue
        scanned += 1
        key = canonical(path)
        marker = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
        next_state[key] = marker
        if state.get(key) != marker:
            changed.append((path, int(stat.st_size)))
    return changed, next_state, scanned


def read_metadata(item: tuple[Path, int]) -> tuple[Path, dict[str, str | float] | None]:
    path, _size = item
    try:
        return path, legacy._get_audio_metadata(path, fallback_title=path.stem, require_playable=True)
    except Exception:
        return path, None


def main() -> int:
    if not acquire_lock():
        print("metadata refresh skipped: previous run is still active")
        return 0
    try:
        init_db()
        state = load_state()
        conn = get_connection()
        try:
            profiles = conn.execute(
                "select station_id, value from station_settings "
                "where key='music_library_folder' and trim(value)<>'' order by station_id"
            ).fetchall()
            default_genres = {
                int(row[0]): str(row[1] or "")
                for row in conn.execute(
                    "select station_id, value from station_settings "
                    "where key='library_default_genre'"
                ).fetchall()
            }
            existing = {}
            for row in conn.execute(
                "select id, station_id, file_path from tracks where track_type='music'"
            ).fetchall():
                existing[(int(row[1]), canonical(row[2]))] = int(row[0])

            updates: list[tuple[int, Path, dict[str, str | float], int | None, str]] = []
            merged_state: dict[str, dict[str, int]] = {}
            scanned = 0
            changed = 0
            cold_start = not state
            for row in profiles:
                station_id = int(row[0])
                folder = str(row[1])
                candidates, profile_state, count = scan_profile(folder, state)
                scanned += count
                merged_state.update(profile_state)
                if cold_start:
                    candidates = [
                        item
                        for item in candidates
                        if (station_id, canonical(item[0])) not in existing
                    ]
                changed += len(candidates)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    for path, metadata in pool.map(read_metadata, candidates):
                        if metadata is None:
                            continue
                        track_id = existing.get((station_id, canonical(path)))
                        updates.append(
                            (
                                station_id,
                                path,
                                metadata,
                                track_id,
                                default_genres.get(station_id, ""),
                            )
                        )

            added = 0
            updated = 0
            for station_id, path, metadata, track_id, default_genre in updates:
                title = str(metadata.get("title") or path.stem or "Track").strip()
                artist = str(metadata.get("artist") or "").strip()
                album = str(metadata.get("album") or "").strip()
                genre = str(metadata.get("genre") or default_genre).strip()
                language = str(metadata.get("language") or "").strip()
                mbid = str(metadata.get("musicbrainz_recordingid") or "").strip()
                duration = float(metadata.get("duration") or 0.0)
                if track_id is None:
                    conn.execute(
                        "insert into tracks "
                        "(station_id,title,artist,album,genre,language,track_type,file_path,is_active,duration,bpm,musicbrainz_recordingid) "
                        "values(?,?,?,?,?,?,?, ?,1,?,0,?)",
                        (station_id, title, artist, album, genre, language, "music", str(path), duration, mbid),
                    )
                    added += 1
                else:
                    conn.execute(
                        "update tracks set title=?,artist=?,album=case when ?<>'' then ? else album end, "
                        "genre=case when ?<>'' then ? else genre end,language=case when ?<>'' then ? else language end, "
                        "file_path=?,is_active=1,duration=?,musicbrainz_recordingid=case when ?<>'' then ? else musicbrainz_recordingid end "
                        "where id=?",
                        (
                            title,
                            artist,
                            album,
                            album,
                            genre,
                            genre,
                            language,
                            language,
                            str(path),
                            duration,
                            mbid,
                            mbid,
                            track_id,
                        ),
                    )
                    updated += 1
            conn.commit()
        finally:
            conn.close()
        save_state(merged_state)
        print(
            f"metadata refresh complete scanned={scanned} changed={changed} "
            f"updated={updated} added={added} interval_seconds={INTERVAL_SECONDS}"
        )
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
