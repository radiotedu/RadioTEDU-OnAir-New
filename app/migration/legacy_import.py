import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from app.db import init_db
from app.repositories.station_output_repo import StationOutputRepository
from app.security.credential_vault import store_system_secret


def _normalize_track_type(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if token in {"ad", "ads"}:
        return "ad"
    if token in {
        "music",
        "jingle",
        "station_id",
        "show",
        "startup",
        "announcement",
    }:
        return token
    return "music"


def _media_dir_for_track_type(track_type: str) -> str:
    token = _normalize_track_type(track_type)
    if token == "jingle":
        return "jingles"
    if token == "ad":
        return "ads"
    return "music"


def _row_dicts(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    cur = conn.cursor()
    cur.execute(query, params)
    return [dict(row) for row in cur.fetchall()]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table_name),),
    )
    return cur.fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    quoted = str(table_name).replace('"', '""')
    return {
        str(row["name"])
        for row in conn.execute(f'PRAGMA table_info("{quoted}")').fetchall()
    }


def _column_or_default(
    columns: set[str],
    name: str,
    default_sql: str,
) -> str:
    return name if name in columns else f"{default_sql} AS {name}"


@contextmanager
def _override_cleanroom_db_path(db_path: Path):
    previous = os.environ.get("CLEANROOM_DB_PATH")
    os.environ["CLEANROOM_DB_PATH"] = str(db_path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CLEANROOM_DB_PATH", None)
        else:
            os.environ["CLEANROOM_DB_PATH"] = previous


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _snapshot_sqlite_database(source: Path, destination: Path) -> None:
    """Create a consistent read-only SQLite snapshot of a live legacy DB."""
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True)
    destination_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()


def _legacy_repo_root(legacy_db_path: Path) -> Path:
    resolved = legacy_db_path.resolve()
    if len(resolved.parents) >= 2:
        return resolved.parents[1]
    return resolved.parent


def _relative_media_subpath(raw_path: str) -> Path | None:
    normalized = str(raw_path or "").strip().replace("\\", "/")
    if not normalized:
        return None
    lower = normalized.lower()
    marker = "/media/"
    index = lower.find(marker)
    if index >= 0:
        rel = normalized[index + len(marker) :].strip("/")
        return Path(rel) if rel else None
    if normalized.startswith("media/"):
        return Path(normalized[len("media/") :])
    return None


def _resolve_legacy_media_source(raw_path: str, legacy_repo_root: Path) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None

    candidate = Path(text)
    if candidate.is_absolute() and candidate.exists():
        return candidate.resolve()

    if not candidate.is_absolute():
        for root in (
            legacy_repo_root,
            legacy_repo_root / "media",
            legacy_repo_root / "uploads",
            legacy_repo_root / "data",
        ):
            probe = (root / candidate).resolve()
            if probe.exists() and probe.is_file():
                return probe

    rel = _relative_media_subpath(text)
    if rel is not None:
        probe = (legacy_repo_root / "media" / rel).resolve()
        if probe.exists() and probe.is_file():
            return probe

    filename = candidate.name
    if filename:
        matches = list((legacy_repo_root / "media").rglob(filename))
        if len(matches) == 1 and matches[0].is_file():
            return matches[0].resolve()
    return None


def _fallback_media_subpath(
    raw_path: str,
    station_id: int,
    station_slug: str,
    track_type: str,
    track_id: int,
) -> Path:
    original = Path(str(raw_path or "").strip())
    filename = original.name or f"track-{int(track_id)}.bin"
    bucket = _media_dir_for_track_type(track_type)
    slug = str(station_slug or "").strip() or f"station-{int(station_id)}"
    if int(station_id) <= 1 and slug in {"main", "default", "station-1"}:
        return Path(bucket) / filename
    return Path("stations") / slug / bucket / filename


def _resolve_imported_file_path(
    raw_path: str,
    *,
    legacy_repo_root: Path,
    media_destination_root: Path,
    copy_media: bool,
    station_id: int,
    station_slug: str,
    track_type: str,
    track_id: int,
    warnings: list[str],
) -> tuple[str, bool]:
    source = _resolve_legacy_media_source(raw_path, legacy_repo_root)
    rel = _relative_media_subpath(raw_path)
    if rel is None:
        rel = _fallback_media_subpath(
            raw_path=raw_path,
            station_id=station_id,
            station_slug=station_slug,
            track_type=track_type,
            track_id=track_id,
        )

    if source is None:
        warnings.append(
            f"track {int(track_id)} media source could not be resolved: {raw_path}"
        )
        if copy_media:
            return str((media_destination_root / rel).resolve()), False
        return str(raw_path or ""), False

    if not copy_media:
        return str(source.resolve()), False

    destination = (media_destination_root / rel).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(destination))
    return str(destination), True


def _parse_bool(raw, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    token = "" if raw is None else str(raw).strip().lower()
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


def _legacy_days(raw: str) -> set[int]:
    text = str(raw or "*").strip()
    if not text or text == "*":
        return set(range(7))
    days: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            day = int(token)
        except ValueError:
            continue
        if 0 <= day <= 6:
            days.add(day)
    return days or set(range(7))


def _parse_clock(raw: str, default_hour: int = 0) -> time:
    text = str(raw or "").strip()
    try:
        hh, mm = text.split(":", 1)
        return time(hour=max(0, min(23, int(hh))), minute=max(0, min(59, int(mm))))
    except Exception:
        return time(hour=default_hour, minute=0)


def _schedule_window(
    day_of_week: str,
    start_time_text: str,
    end_time_text: str,
) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    allowed_days = _legacy_days(day_of_week)
    start_clock = _parse_clock(start_time_text, default_hour=0)
    end_clock = _parse_clock(end_time_text, default_hour=(start_clock.hour + 1) % 24)

    for day_offset in range(0, 8):
        current_date = (now + timedelta(days=day_offset)).date()
        if current_date.weekday() not in allowed_days:
            continue
        start_at = datetime.combine(current_date, start_clock, tzinfo=timezone.utc)
        end_at = datetime.combine(current_date, end_clock, tzinfo=timezone.utc)
        if end_at <= start_at:
            end_at = end_at + timedelta(days=1)
        if day_offset > 0 or end_at >= now:
            return (
                start_at.isoformat().replace("+00:00", "Z"),
                end_at.isoformat().replace("+00:00", "Z"),
            )

    fallback_start = now.replace(
        hour=start_clock.hour,
        minute=start_clock.minute,
        second=0,
        microsecond=0,
    )
    fallback_end = fallback_start + timedelta(hours=1)
    return (
        fallback_start.isoformat().replace("+00:00", "Z"),
        fallback_end.isoformat().replace("+00:00", "Z"),
    )


def _track_snapshot_map(track_rows: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in track_rows:
        tid = int(row.get("id") or 0)
        if tid <= 0:
            continue
        out[tid] = {
            "track_id": tid,
            "id": tid,
            "title": str(row.get("title") or ""),
            "artist": str(row.get("artist") or ""),
            "duration": float(row.get("duration") or 0.0),
        }
    return out


def _import_stations(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    summary: dict,
    warnings: list[str],
) -> dict[int, str]:
    station_slugs: dict[int, str] = {}
    if not _table_exists(legacy_conn, "stations"):
        warnings.append("legacy table missing: stations")
        return station_slugs

    station_columns = _table_columns(legacy_conn, "stations")
    slug_expr = "slug" if "slug" in station_columns else "'' AS slug"
    active_filter = (
        "WHERE COALESCE(is_active, 1) <> 0"
        if "is_active" in station_columns
        else ""
    )
    rows = _row_dicts(
        legacy_conn,
        f"SELECT id, name, {slug_expr} FROM stations {active_filter} ORDER BY id ASC",
    )
    cur = clean_conn.cursor()
    for row in rows:
        sid = int(row.get("id") or 0)
        if sid <= 0:
            continue
        station_slugs[sid] = str(row.get("slug") or "").strip() or f"station-{sid}"
        cur.execute(
            "INSERT INTO stations (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (sid, str(row.get("name") or f"Station {sid}").strip()),
        )
    summary["stations"] = len(rows)
    return station_slugs


def _import_tracks(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    *,
    legacy_repo_root: Path,
    media_destination_root: Path,
    copy_media: bool,
    station_slugs: dict[int, str],
    summary: dict,
    warnings: list[str],
) -> list[dict]:
    if not _table_exists(legacy_conn, "tracks"):
        warnings.append("legacy table missing: tracks")
        return []

    rows = _row_dicts(
        legacy_conn,
        "SELECT id, station_id, file_path, title, artist, album, genre, language, bpm, duration, track_type, is_active "
        "FROM tracks ORDER BY id ASC",
    )
    cur = clean_conn.cursor()
    copied = 0
    for row in rows:
        tid = int(row.get("id") or 0)
        if tid <= 0:
            continue
        station_id = int(row.get("station_id") or 1)
        file_path, did_copy = _resolve_imported_file_path(
            str(row.get("file_path") or ""),
            legacy_repo_root=legacy_repo_root,
            media_destination_root=media_destination_root,
            copy_media=copy_media,
            station_id=station_id,
            station_slug=station_slugs.get(station_id, f"station-{station_id}"),
            track_type=str(row.get("track_type") or "music"),
            track_id=tid,
            warnings=warnings,
        )
        copied += 1 if did_copy else 0
        cur.execute(
            "INSERT INTO tracks (id, station_id, title, artist, album, genre, language, duration, bpm, track_type, is_active, musicbrainz_recordingid, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "station_id=excluded.station_id, title=excluded.title, artist=excluded.artist, album=excluded.album, "
            "genre=excluded.genre, language=excluded.language, duration=excluded.duration, bpm=excluded.bpm, "
            "track_type=excluded.track_type, is_active=excluded.is_active, file_path=excluded.file_path",
            (
                tid,
                station_id,
                str(row.get("title") or ""),
                str(row.get("artist") or ""),
                str(row.get("album") or ""),
                str(row.get("genre") or ""),
                str(row.get("language") or ""),
                float(row.get("duration") or 0.0),
                float(row.get("bpm") or 0.0),
                _normalize_track_type(str(row.get("track_type") or "music")),
                1 if _parse_bool(row.get("is_active", 1), True) else 0,
                file_path,
            ),
        )
        row["file_path"] = file_path
        row["track_type"] = _normalize_track_type(str(row.get("track_type") or "music"))
    summary["tracks"] = len(rows)
    summary["media_copied"] = copied
    return rows


def _import_playlists(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    summary: dict,
    warnings: list[str],
) -> None:
    cur = clean_conn.cursor()

    if _table_exists(legacy_conn, "playlists"):
        playlist_columns = _table_columns(legacy_conn, "playlists")
        description_expr = (
            "description" if "description" in playlist_columns else "'' AS description"
        )
        playlist_type_expr = (
            "playlist_type"
            if "playlist_type" in playlist_columns
            else "'manual' AS playlist_type"
        )
        created_at_expr = (
            "created_at"
            if "created_at" in playlist_columns
            else "'1970-01-01 00:00:00' AS created_at"
        )
        active_filter = (
            "WHERE COALESCE(is_active, 1) <> 0"
            if "is_active" in playlist_columns
            else ""
        )
        rows = _row_dicts(
            legacy_conn,
            f"SELECT id, station_id, name, {description_expr}, "
            f"{playlist_type_expr}, {created_at_expr} FROM playlists "
            f"{active_filter} ORDER BY id ASC",
        )
        for row in rows:
            cur.execute(
                "INSERT INTO playlists (id, station_id, name, description, playlist_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "station_id=excluded.station_id, name=excluded.name, description=excluded.description, "
                "playlist_type=excluded.playlist_type, created_at=excluded.created_at",
                (
                    int(row.get("id") or 0),
                    int(row.get("station_id") or 1),
                    str(row.get("name") or ""),
                    str(row.get("description") or ""),
                    str(row.get("playlist_type") or "manual"),
                    str(row.get("created_at") or "1970-01-01 00:00:00"),
                ),
            )
        summary["playlists"] = len(rows)
    else:
        warnings.append("legacy table missing: playlists")

    if _table_exists(legacy_conn, "playlist_items"):
        item_columns = _table_columns(legacy_conn, "playlist_items")
        added_at_expr = (
            "added_at"
            if "added_at" in item_columns
            else (
                "created_at AS added_at"
                if "created_at" in item_columns
                else "'1970-01-01 00:00:00' AS added_at"
            )
        )
        rows = _row_dicts(
            legacy_conn,
            f"SELECT id, playlist_id, track_id, position, {added_at_expr} "
            "FROM playlist_items ORDER BY id ASC",
        )
        for row in rows:
            cur.execute(
                "INSERT INTO playlist_items (id, playlist_id, track_id, position, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "playlist_id=excluded.playlist_id, track_id=excluded.track_id, position=excluded.position, created_at=excluded.created_at",
                (
                    int(row.get("id") or 0),
                    int(row.get("playlist_id") or 0),
                    int(row.get("track_id") or 0),
                    int(row.get("position") or 0),
                    str(row.get("added_at") or "1970-01-01 00:00:00"),
                ),
            )
        summary["playlist_items"] = len(rows)
    else:
        warnings.append("legacy table missing: playlist_items")


def _import_system_settings(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    summary: dict,
    warnings: list[str],
) -> None:
    if not _table_exists(legacy_conn, "system_settings"):
        warnings.append("legacy table missing: system_settings")
        return

    columns = _table_columns(legacy_conn, "system_settings")
    key_column = "setting_key" if "setting_key" in columns else "key"
    value_column = "setting_value" if "setting_value" in columns else "value"
    updated_expr = (
        "updated_at"
        if "updated_at" in columns
        else "'1970-01-01 00:00:00' AS updated_at"
    )
    rows = _row_dicts(
        legacy_conn,
        f"SELECT {key_column} AS setting_key, {value_column} AS setting_value, "
        f"{updated_expr} FROM system_settings ORDER BY {key_column} ASC",
    )
    cur = clean_conn.cursor()
    for row in rows:
        key = str(row.get("setting_key") or "")
        value = str(row.get("setting_value") or "")
        if key in {
            "rocket_admin_password",
            "rocket_health_password",
            "radiotedu_voting_agent_token",
        } and value:
            value = store_system_secret(key, value)
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                key,
                value,
                str(row.get("updated_at") or "1970-01-01 00:00:00"),
            ),
        )
    summary["system_settings"] = len(rows)


def _import_station_settings(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    summary: dict,
    warnings: list[str],
) -> dict[int, dict[str, str]]:
    settings_by_station: dict[int, dict[str, str]] = defaultdict(dict)
    if not _table_exists(legacy_conn, "station_settings"):
        warnings.append("legacy table missing: station_settings")
        return settings_by_station

    columns = _table_columns(legacy_conn, "station_settings")
    key_column = "setting_key" if "setting_key" in columns else "key"
    value_column = "setting_value" if "setting_value" in columns else "value"
    updated_expr = (
        "updated_at"
        if "updated_at" in columns
        else "'1970-01-01 00:00:00' AS updated_at"
    )
    rows = _row_dicts(
        legacy_conn,
        f"SELECT station_id, {key_column} AS setting_key, "
        f"{value_column} AS setting_value, {updated_expr} "
        f"FROM station_settings ORDER BY station_id ASC, {key_column} ASC",
    )
    cur = clean_conn.cursor()
    for row in rows:
        station_id = int(row.get("station_id") or 1)
        key = str(row.get("setting_key") or "")
        value = str(row.get("setting_value") or "")
        settings_by_station[station_id][key] = value
        stored_value = "" if key == "icecast_password" else value
        cur.execute(
            "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                station_id,
                key,
                stored_value,
                str(row.get("updated_at") or "1970-01-01 00:00:00"),
            ),
        )
    summary["station_settings"] = len(rows)
    return settings_by_station


def _import_station_outputs(
    clean_conn: sqlite3.Connection,
    settings_by_station: dict[int, dict[str, str]],
    summary: dict,
) -> None:
    output_repo = StationOutputRepository(clean_conn)
    count = 0
    for station_id in sorted(settings_by_station):
        settings = dict(settings_by_station[station_id])
        output_mode = str(settings.get("output_mode", "speaker") or "speaker").strip().lower()
        speaker_monitor_enabled = _parse_bool(
            settings.get("speaker_monitor_enabled", True),
            True,
        )
        icecast_enabled = output_mode == "icecast"
        local_output_enabled = speaker_monitor_enabled if icecast_enabled else True
        output_repo.upsert(
            station_id=int(station_id),
            local_output_enabled=local_output_enabled,
            output_device_id=str(settings.get("output_device_id", "") or "").strip(),
            icecast_enabled=icecast_enabled,
            icecast_host=str(settings.get("icecast_host", "127.0.0.1") or "127.0.0.1"),
            icecast_port=_parse_int(settings.get("icecast_port", 8000), 8000),
            icecast_mount=str(
                settings.get("icecast_mount", f"/station{station_id}")
                or f"/station{station_id}"
            ),
            icecast_user=str(
                settings.get("icecast_username", settings.get("icecast_user", "source"))
                or "source"
            ),
            icecast_password=str(settings.get("icecast_password", "") or ""),
            output_gain_db=_parse_float(settings.get("output_gain_db", 0.0), 0.0),
        )
        count += 1
    summary["station_outputs"] = count


def _import_metadata_rules(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    summary: dict,
    warnings: list[str],
) -> None:
    if not _table_exists(legacy_conn, "metadata_rules"):
        warnings.append("legacy table missing: metadata_rules")
        return

    rows = _row_dicts(
        legacy_conn,
        "SELECT id, station_id, scope, name, target_field, match_type, pattern, replacement, is_case_sensitive, priority, is_active, created_at, updated_at "
        "FROM metadata_rules ORDER BY id ASC",
    )
    cur = clean_conn.cursor()
    for row in rows:
        cur.execute(
            "INSERT INTO metadata_rules (id, station_id, scope, name, target_field, match_type, pattern, replacement, is_case_sensitive, priority, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "station_id=excluded.station_id, scope=excluded.scope, name=excluded.name, target_field=excluded.target_field, "
            "match_type=excluded.match_type, pattern=excluded.pattern, replacement=excluded.replacement, "
            "is_case_sensitive=excluded.is_case_sensitive, priority=excluded.priority, is_active=excluded.is_active, "
            "created_at=excluded.created_at, updated_at=excluded.updated_at",
            (
                int(row.get("id") or 0),
                int(row.get("station_id") or 0) or None,
                str(row.get("scope") or "station"),
                str(row.get("name") or ""),
                str(row.get("target_field") or "title"),
                str(row.get("match_type") or "contains"),
                str(row.get("pattern") or ""),
                str(row.get("replacement") or ""),
                1 if _parse_bool(row.get("is_case_sensitive", 0), False) else 0,
                _parse_int(row.get("priority", 100), 100),
                1 if _parse_bool(row.get("is_active", 1), True) else 0,
                str(row.get("created_at") or "1970-01-01 00:00:00"),
                str(row.get("updated_at") or "1970-01-01 00:00:00"),
            ),
        )
    summary["metadata_rules"] = len(rows)


def _import_schedule(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    summary: dict,
    warnings: list[str],
) -> None:
    if not _table_exists(legacy_conn, "schedule"):
        warnings.append("legacy table missing: schedule")
        return

    rows = _row_dicts(
        legacy_conn,
        "SELECT id, station_id, playlist_id, event_name, day_of_week, start_time, end_time, is_active FROM schedule ORDER BY id ASC",
    )
    cur = clean_conn.cursor()
    for row in rows:
        play_at, window_end = _schedule_window(
            day_of_week=str(row.get("day_of_week") or "*"),
            start_time_text=str(row.get("start_time") or "00:00"),
            end_time_text=str(row.get("end_time") or "01:00"),
        )
        cur.execute(
            "INSERT INTO schedule_items (id, station_id, track_id, play_at, window_end, status) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "station_id=excluded.station_id, track_id=excluded.track_id, play_at=excluded.play_at, "
            "window_end=excluded.window_end, status=excluded.status",
            (
                int(row.get("id") or 0),
                int(row.get("station_id") or 1),
                int(row.get("playlist_id") or 0),
                play_at,
                window_end,
                "pending" if _parse_bool(row.get("is_active", 1), True) else "failed",
            ),
        )
        warnings.append(
            f"schedule {int(row.get('id') or 0)} imported as next upcoming one-shot window"
        )
    summary["schedule_items"] = len(rows)


def _load_slot_rows(legacy_conn: sqlite3.Connection) -> tuple[dict[int, list[dict]], dict[int, dict]]:
    slots_by_break_set: dict[int, list[dict]] = defaultdict(list)
    slots_by_id: dict[int, dict] = {}
    if not _table_exists(legacy_conn, "ad_break_slots"):
        return slots_by_break_set, slots_by_id

    rows = _row_dicts(
        legacy_conn,
        "SELECT id, break_set_id, slot_time, day_of_week, position, is_active FROM ad_break_slots ORDER BY break_set_id ASC, position ASC, id ASC",
    )
    for row in rows:
        slot = {
            "slot_id": int(row.get("id") or 0),
            "break_set_id": int(row.get("break_set_id") or 0),
            "slot_time": str(row.get("slot_time") or "").strip()[:5],
            "day_of_week": str(row.get("day_of_week") or "*").strip() or "*",
            "position": int(row.get("position") or 0),
            "is_active": _parse_bool(row.get("is_active", 1), True),
        }
        slots_by_id[slot["slot_id"]] = slot
        slots_by_break_set[slot["break_set_id"]].append(slot)
    return slots_by_break_set, slots_by_id


def _import_ads(
    legacy_conn: sqlite3.Connection,
    clean_conn: sqlite3.Connection,
    track_rows: list[dict],
    summary: dict,
    warnings: list[str],
) -> None:
    slots_by_break_set, slots_by_id = _load_slot_rows(legacy_conn)
    track_map = _track_snapshot_map(track_rows)
    cur = clean_conn.cursor()

    if _table_exists(legacy_conn, "ad_break_sets"):
        columns = _table_columns(legacy_conn, "ad_break_sets")
        select_columns = [
            "id",
            "station_id",
            "name",
            _column_or_default(columns, "description", "''"),
            _column_or_default(columns, "intro_jingle_track_id", "NULL"),
            _column_or_default(columns, "outro_jingle_track_id", "NULL"),
            _column_or_default(columns, "is_active", "1"),
            _column_or_default(
                columns, "created_at", "'1970-01-01 00:00:00'"
            ),
            _column_or_default(
                columns, "updated_at", "'1970-01-01 00:00:00'"
            ),
        ]
        rows = _row_dicts(
            legacy_conn,
            f"SELECT {', '.join(select_columns)} "
            "FROM ad_break_sets ORDER BY id ASC",
        )
        for row in rows:
            payload = {
                "description": str(row.get("description") or ""),
                "is_active": _parse_bool(row.get("is_active", 1), True),
                "slots": [
                    {
                        "slot_time": str(slot.get("slot_time") or ""),
                        "day_of_week": str(slot.get("day_of_week") or "*"),
                        "position": int(slot.get("position") or 0),
                        "is_active": bool(slot.get("is_active", True)),
                    }
                    for slot in slots_by_break_set.get(int(row.get("id") or 0), [])
                ],
                "intro_jingle_track_id": int(row.get("intro_jingle_track_id") or 0) or None,
                "outro_jingle_track_id": int(row.get("outro_jingle_track_id") or 0) or None,
            }
            cur.execute(
                "INSERT INTO ad_break_sets (id, station_id, name, enabled, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "station_id=excluded.station_id, name=excluded.name, enabled=excluded.enabled, "
                "payload_json=excluded.payload_json, created_at=excluded.created_at, updated_at=excluded.updated_at",
                (
                    int(row.get("id") or 0),
                    int(row.get("station_id") or 1),
                    str(row.get("name") or ""),
                    1 if payload["is_active"] else 0,
                    json.dumps(payload),
                    str(row.get("created_at") or "1970-01-01 00:00:00"),
                    str(row.get("updated_at") or "1970-01-01 00:00:00"),
                ),
            )
        summary["ad_break_sets"] = len(rows)
    else:
        warnings.append("legacy table missing: ad_break_sets")

    campaign_track_ids: dict[int, list[int]] = defaultdict(list)
    if _table_exists(legacy_conn, "ad_campaign_items"):
        columns = _table_columns(legacy_conn, "ad_campaign_items")
        order_columns = ["campaign_id"]
        if "position" in columns:
            order_columns.append("position")
        if "id" in columns:
            order_columns.append("id")
        item_rows = _row_dicts(
            legacy_conn,
            "SELECT campaign_id, track_id FROM ad_campaign_items ORDER BY "
            + ", ".join(f"{name} ASC" for name in order_columns),
        )
        for row in item_rows:
            campaign_track_ids[int(row.get("campaign_id") or 0)].append(int(row.get("track_id") or 0))

    campaign_slot_ids: dict[int, list[int]] = defaultdict(list)
    if _table_exists(legacy_conn, "ad_campaign_slots"):
        columns = _table_columns(legacy_conn, "ad_campaign_slots")
        order_columns = ["campaign_id"]
        if "id" in columns:
            order_columns.append("id")
        slot_rows = _row_dicts(
            legacy_conn,
            "SELECT campaign_id, slot_id FROM ad_campaign_slots ORDER BY "
            + ", ".join(f"{name} ASC" for name in order_columns),
        )
        for row in slot_rows:
            campaign_slot_ids[int(row.get("campaign_id") or 0)].append(int(row.get("slot_id") or 0))

    if _table_exists(legacy_conn, "ad_campaigns"):
        columns = _table_columns(legacy_conn, "ad_campaigns")
        select_columns = [
            "id",
            "station_id",
            "name",
            _column_or_default(columns, "start_date", "''"),
            _column_or_default(columns, "end_date", "''"),
            _column_or_default(columns, "play_times", "''"),
            _column_or_default(columns, "day_interval", "1"),
            _column_or_default(columns, "daily_repeat_limit", "0"),
            _column_or_default(columns, "priority", "0"),
            _column_or_default(columns, "is_active", "1"),
            _column_or_default(columns, "notes", "''"),
            _column_or_default(
                columns, "created_at", "'1970-01-01 00:00:00'"
            ),
            _column_or_default(
                columns, "updated_at", "'1970-01-01 00:00:00'"
            ),
        ]
        rows = _row_dicts(
            legacy_conn,
            f"SELECT {', '.join(select_columns)} "
            "FROM ad_campaigns ORDER BY id ASC",
        )
        for row in rows:
            campaign_id = int(row.get("id") or 0)
            slot_ids = [slot_id for slot_id in campaign_slot_ids.get(campaign_id, []) if slot_id > 0]
            track_ids = [track_id for track_id in campaign_track_ids.get(campaign_id, []) if track_id > 0]
            payload = {
                "is_active": _parse_bool(row.get("is_active", 1), True),
                "start_date": str(row.get("start_date") or ""),
                "end_date": str(row.get("end_date") or ""),
                "day_interval": max(1, _parse_int(row.get("day_interval", 1), 1)),
                "daily_repeat_limit": max(0, _parse_int(row.get("daily_repeat_limit", 0), 0)),
                "priority": _parse_int(row.get("priority", 0), 0),
                "notes": str(row.get("notes") or ""),
                "play_times": str(row.get("play_times") or ""),
                "slot_ids": slot_ids,
                "track_ids": track_ids,
                "slots": [slots_by_id[slot_id] for slot_id in slot_ids if slot_id in slots_by_id],
                "tracks": [track_map[track_id] for track_id in track_ids if track_id in track_map],
            }
            cur.execute(
                "INSERT INTO ad_campaigns (id, station_id, name, enabled, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "station_id=excluded.station_id, name=excluded.name, enabled=excluded.enabled, "
                "payload_json=excluded.payload_json, created_at=excluded.created_at, updated_at=excluded.updated_at",
                (
                    campaign_id,
                    int(row.get("station_id") or 1),
                    str(row.get("name") or ""),
                    1 if payload["is_active"] else 0,
                    json.dumps(payload),
                    str(row.get("created_at") or "1970-01-01 00:00:00"),
                    str(row.get("updated_at") or "1970-01-01 00:00:00"),
                ),
            )
        summary["ad_campaigns"] = len(rows)
    else:
        warnings.append("legacy table missing: ad_campaigns")


def import_legacy_database(
    legacy_db_path: str | Path,
    cleanroom_db_path: str | Path,
    *,
    copy_media: bool = False,
    media_destination_root: str | Path | None = None,
    legacy_repo_root: str | Path | None = None,
) -> dict:
    legacy_db = Path(legacy_db_path).expanduser().resolve()
    cleanroom_db = Path(cleanroom_db_path).expanduser().resolve()
    if not legacy_db.exists():
        raise FileNotFoundError(f"legacy database not found: {legacy_db}")

    cleanroom_db.parent.mkdir(parents=True, exist_ok=True)
    media_root = (
        Path(media_destination_root).expanduser().resolve()
        if media_destination_root is not None
        else (cleanroom_db.parent / "media").resolve()
    )

    with _override_cleanroom_db_path(cleanroom_db):
        init_db()

    legacy_conn = _connect_sqlite(legacy_db)
    clean_conn = _connect_sqlite(cleanroom_db)
    warnings: list[str] = []
    summary = {
        "stations": 0,
        "tracks": 0,
        "playlists": 0,
        "playlist_items": 0,
        "schedule_items": 0,
        "system_settings": 0,
        "station_settings": 0,
        "station_outputs": 0,
        "metadata_rules": 0,
        "ad_break_sets": 0,
        "ad_campaigns": 0,
        "media_copied": 0,
        "warnings": warnings,
    }

    try:
        station_slugs = _import_stations(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            summary=summary,
            warnings=warnings,
        )
        track_rows = _import_tracks(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            legacy_repo_root=(
                Path(legacy_repo_root).expanduser().resolve()
                if legacy_repo_root is not None
                else _legacy_repo_root(legacy_db)
            ),
            media_destination_root=media_root,
            copy_media=bool(copy_media),
            station_slugs=station_slugs,
            summary=summary,
            warnings=warnings,
        )
        _import_playlists(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            summary=summary,
            warnings=warnings,
        )
        _import_system_settings(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            summary=summary,
            warnings=warnings,
        )
        settings_by_station = _import_station_settings(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            summary=summary,
            warnings=warnings,
        )
        _import_station_outputs(
            clean_conn=clean_conn,
            settings_by_station=settings_by_station,
            summary=summary,
        )
        _import_metadata_rules(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            summary=summary,
            warnings=warnings,
        )
        _import_schedule(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            summary=summary,
            warnings=warnings,
        )
        _import_ads(
            legacy_conn=legacy_conn,
            clean_conn=clean_conn,
            track_rows=track_rows,
            summary=summary,
            warnings=warnings,
        )
        clean_conn.commit()
        return summary
    finally:
        clean_conn.close()
        legacy_conn.close()


def migrate_legacy_database_safely(
    legacy_db_path: str | Path,
    cleanroom_db_path: str | Path,
    *,
    dry_run: bool = True,
    copy_media: bool = False,
    media_destination_root: str | Path | None = None,
) -> dict:
    """Stage and validate an import before atomically replacing the target DB."""
    legacy_db = Path(legacy_db_path).expanduser().resolve()
    target_db = Path(cleanroom_db_path).expanduser().resolve()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    staging_db = target_db.with_name(
        f".{target_db.name}.migration-{uuid.uuid4().hex}.tmp"
    )
    backup_path: Path | None = None
    original_vault_override = os.environ.get("CLEANROOM_CREDENTIAL_STORE_FILE")
    temporary_root = Path(tempfile.mkdtemp(prefix="radiotedu-onair-migration-"))
    legacy_snapshot = temporary_root / "legacy-snapshot.db"

    try:
        _snapshot_sqlite_database(legacy_db, legacy_snapshot)
        if target_db.exists():
            shutil.copy2(target_db, staging_db)
        if dry_run:
            os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(
                temporary_root / "credentials.json"
            )

        summary = import_legacy_database(
            legacy_snapshot,
            staging_db,
            copy_media=bool(copy_media and not dry_run),
            media_destination_root=media_destination_root,
            legacy_repo_root=_legacy_repo_root(legacy_db),
        )
        summary = dict(summary)
        summary.update(
            {
                "dry_run": bool(dry_run),
                "source_database": str(legacy_db),
                "target_database": str(target_db),
                "target_replaced": False,
                "backup_database": "",
                "source_snapshot_used": True,
            }
        )
        if dry_run:
            return summary

        if target_db.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = target_db.with_name(
                f"{target_db.name}.backup-{timestamp}"
            )
            shutil.copy2(target_db, backup_path)
        os.replace(staging_db, target_db)
        summary["target_replaced"] = True
        summary["backup_database"] = str(backup_path or "")
        return summary
    finally:
        if original_vault_override is None:
            os.environ.pop("CLEANROOM_CREDENTIAL_STORE_FILE", None)
        else:
            os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = original_vault_override
        try:
            staging_db.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(temporary_root, ignore_errors=True)
