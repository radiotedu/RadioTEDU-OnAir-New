import threading

from app.repositories.queue_repo import QueueRepository
from app.repositories.settings_repo import SettingsRepository

_QUEUE_AUTOFILL_LOCK = threading.Lock()


def _get_library_fallback_track_id(conn, station_id: int) -> int | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT current_item_id FROM playout_state "
        "WHERE station_id=? AND current_source='library_fallback' LIMIT 1",
        (int(station_id),),
    )
    row = cur.fetchone()
    if row is None or row["current_item_id"] is None:
        return None
    return int(row["current_item_id"])


def _get_sweeper_settings(conn, station_id: int) -> dict:
    """Return sweeper configuration for the station."""
    settings = SettingsRepository(conn).get_station(int(station_id))
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
    return {"enabled": enabled, "interval": interval, "interval_unit": interval_unit, "mode": mode}


def _pick_rotation_track(
    conn,
    station_id: int,
    track_type: str,
    mode: str = "random",
    exclude_ids: set[int] | None = None,
) -> dict | None:
    """Select the least-used active jingle or ad for deterministic rotation."""
    normalized_type = str(track_type or "").strip().lower()
    if normalized_type not in {"jingle", "ad"}:
        raise ValueError("rotation track_type must be jingle or ad")
    blocked = sorted({int(x) for x in (exclude_ids or set()) if int(x) > 0})
    where = [
        "station_id=?",
        "is_active=1",
        "LOWER(COALESCE(track_type, 'music'))=?",
        "COALESCE(file_path, '') <> ''",
    ]
    params: list = [int(station_id), normalized_type]
    if blocked:
        placeholders = ",".join("?" for _ in blocked)
        where.append(f"id NOT IN ({placeholders})")
        params.extend(blocked)
    query = (
        "SELECT id, title, artist, duration, track_type, file_path "
        "FROM tracks WHERE "
        + " AND ".join(where)
        + " ORDER BY COALESCE(play_count, 0) ASC, "
        + "CASE WHEN last_played_at IS NULL OR TRIM(last_played_at)='' THEN 0 ELSE 1 END ASC, "
        + "last_played_at ASC, id ASC LIMIT 1"
    )
    cur = conn.cursor()
    cur.execute(query, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    if not rows and blocked:
        return _pick_rotation_track(
            conn,
            station_id,
            normalized_type,
            mode=mode,
            exclude_ids=None,
        )
    if not rows:
        return None
    return rows[0]


def _pick_jingle(
    conn,
    station_id: int,
    mode: str = "random",
    exclude_ids: set[int] | None = None,
) -> dict | None:
    return _pick_rotation_track(
        conn, station_id, "jingle", mode=mode, exclude_ids=exclude_ids
    )


def _pick_ad(
    conn,
    station_id: int,
    mode: str = "ordered",
    exclude_ids: set[int] | None = None,
) -> dict | None:
    return _pick_rotation_track(
        conn, station_id, "ad", mode=mode, exclude_ids=exclude_ids
    )


def _count_music_since_last_jingle(conn, station_id: int) -> int:
    """Count how many consecutive music tracks have played since the last jingle."""
    cur = conn.cursor()
    # Use immutable queue ids for playback recency; positions may be reindexed later.
    cur.execute(
        "SELECT q.id, LOWER(COALESCE(t.track_type, 'music')) AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=? AND q.status IN ('done', 'playing') "
        "ORDER BY q.id DESC LIMIT 50",
        (int(station_id),),
    )
    count = 0
    for row in cur.fetchall():
        if row["track_type"] == "jingle":
            break
        if row["track_type"] == "music":
            count += 1
    return count


def _recent_jingle_track_ids(conn, station_id: int, limit: int = 1) -> list[int]:
    cur = conn.cursor()
    cur.execute(
        "SELECT q.track_id "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=? AND q.status IN ('done', 'playing') "
        "AND LOWER(COALESCE(t.track_type, 'music'))='jingle' "
        "ORDER BY q.id DESC LIMIT ?",
        (int(station_id), max(1, int(limit))),
    )
    return [int(row["track_id"]) for row in cur.fetchall() if int(row["track_id"] or 0) > 0]


def _recent_ad_track_ids(conn, station_id: int, limit: int = 1) -> list[int]:
    cur = conn.cursor()
    cur.execute(
        "SELECT q.track_id FROM queue_items q "
        "LEFT JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=? AND q.status IN ('done', 'playing') "
        "AND LOWER(COALESCE(t.track_type, 'music'))='ad' "
        "ORDER BY q.id DESC LIMIT ?",
        (int(station_id), max(1, int(limit))),
    )
    return [int(row["track_id"]) for row in cur.fetchall() if int(row["track_id"] or 0) > 0]


def _purge_inactive_pending_queue_items(conn, station_id: int) -> int:
    """Remove pending rows whose library track was deleted or deactivated.

    A playing row is deliberately preserved so a live source is never cut mid-track.
    This also prevents already-queued jingles from collapsing together when deleted
    music rows disappear from a replace-managed live folder.
    """
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM queue_items AS q "
        "WHERE q.station_id=? AND q.status='pending' AND ("
        "  NOT EXISTS (SELECT 1 FROM tracks t WHERE t.id=q.track_id) OR "
        "  EXISTS (SELECT 1 FROM tracks t WHERE t.id=q.track_id "
        "          AND COALESCE(t.is_active,0)=0 "
        "          AND LOWER(COALESCE(t.track_type,'music'))<>'announcement')"
        ")",
        (int(station_id),),
    )
    removed = int(cur.rowcount or 0)
    # SQLite opens a write transaction even when the DELETE matches no rows.
    # Leaving that empty transaction open makes QueueRepository believe an
    # outer transaction owns the subsequent enqueue, so it neither commits
    # the new queue rows nor releases the database lock.
    if conn.in_transaction:
        conn.commit()
    return removed


def select_broadcast_queue_autofill_tracks(
    conn,
    station_id: int,
    limit: int = 10,
    exclude_ids: set[int] | None = None,
) -> list[dict]:
    blocked = sorted({int(x) for x in (exclude_ids or set()) if int(x) > 0})
    params: list = [int(station_id)]
    where = [
        "station_id=?",
        "is_active=1",
        "LOWER(COALESCE(track_type, 'music'))='music'",
        "COALESCE(file_path, '') <> ''",
        "COALESCE(exclude_from_autoplay, 0)=0",
    ]
    if blocked:
        placeholders = ",".join("?" for _ in blocked)
        where.append(f"id NOT IN ({placeholders})")
        params.extend(blocked)
    query = (
        "SELECT id, title, artist, duration, track_type, file_path "
        "FROM tracks WHERE "
        + " AND ".join(where)
        + " ORDER BY COALESCE(play_count, 0) ASC, "
        + "CASE WHEN last_played_at IS NULL OR TRIM(last_played_at)='' THEN 0 ELSE 1 END ASC, "
        + "last_played_at ASC, id ASC LIMIT ?"
    )
    params.append(max(1, int(limit)))
    cur = conn.cursor()
    cur.execute(query, tuple(params))
    return [dict(row) for row in cur.fetchall()]


def _interleave_with_jingles(
    conn,
    station_id: int,
    music_candidates: list[dict],
    sweeper: dict,
    music_since_jingle: int,
) -> list[dict]:
    """Interleave jingle tracks among music candidates based on sweeper settings."""
    if (
        not sweeper["enabled"]
        or sweeper.get("interval_unit", "tracks") == "minutes"
        or not music_candidates
    ):
        return music_candidates

    interval = sweeper["interval"]
    result: list[dict] = []
    counter = music_since_jingle
    recent_jingle_ids = set(_recent_jingle_track_ids(conn, station_id, limit=1))
    recent_ad_ids = set(_recent_ad_track_ids(conn, station_id, limit=1))
    used_jingle_ids: set[int] = set()
    used_ad_ids: set[int] = set()

    for track in music_candidates:
        # Check if we need a jingle before this music track
        if counter >= interval:
            jingle = _pick_jingle(
                conn,
                station_id,
                sweeper["mode"],
                exclude_ids=recent_jingle_ids | used_jingle_ids,
            )
            if jingle:
                result.append(jingle)
                used_jingle_ids.add(int(jingle["id"]))
                ad = _pick_ad(
                    conn,
                    station_id,
                    exclude_ids=recent_ad_ids | used_ad_ids,
                )
                if ad:
                    result.append(ad)
                    used_ad_ids.add(int(ad["id"]))
                counter = 0
        result.append(track)
        counter += 1

    return result


def reconcile_pending_sweeper_queue(conn, station_id: int) -> list[dict]:
    """Apply current sweeper settings to existing active queue rows."""
    inactive_removed = _purge_inactive_pending_queue_items(conn, int(station_id))
    repo = QueueRepository(conn)
    sweeper = _get_sweeper_settings(conn, int(station_id))
    cur = conn.cursor()

    # Delete ALL pending jingles — both those with dedupe_key='jingle:...'
    # and those inserted without a dedupe_key (e.g. by station_worker autofill).
    cur.execute(
        "DELETE FROM queue_items "
        "WHERE station_id=? AND status='pending' AND id IN ("
        "  SELECT q.id FROM queue_items q "
        "  LEFT JOIN tracks t ON t.id = q.track_id "
        "  WHERE q.station_id=? AND q.status='pending' "
        "  AND LOWER(COALESCE(t.track_type, 'music')) IN ('jingle', 'ad')"
        ")",
        (int(station_id), int(station_id)),
    )
    removed = inactive_removed + int(cur.rowcount or 0)

    rows = list(repo.list_active_ordered(station_id=station_id))
    playing_rows = [row for row in rows if str(row["status"]) == "playing"]
    pending_rows = [row for row in rows if str(row["status"]) == "pending"]

    next_pos = 1
    for row in playing_rows:
        cur.execute("UPDATE queue_items SET position=? WHERE id=?", (next_pos, int(row["id"])))
        next_pos += 1

    if not sweeper["enabled"]:
        for row in pending_rows:
            cur.execute("UPDATE queue_items SET position=? WHERE id=?", (next_pos, int(row["id"])))
            next_pos += 1
        if removed > 0:
            conn.commit()
        return list(repo.list_active_ordered(station_id=station_id))

    # Minute-based automation is inserted by StationWorker at the boundary
    # after the song that crosses the threshold. Do not pre-place it early.
    if sweeper.get("interval_unit", "tracks") == "minutes":
        for row in pending_rows:
            cur.execute("UPDATE queue_items SET position=? WHERE id=?", (next_pos, int(row["id"])))
            next_pos += 1
        conn.commit()
        if not pending_rows:
            return ensure_broadcast_queue_ready_for_playback(
                conn,
                station_id=int(station_id),
                allow_when_only_playing=True,
            )
        return list(repo.list_active_ordered(station_id=station_id))

    if not pending_rows:
        conn.commit()
        return ensure_broadcast_queue_ready_for_playback(
            conn,
            station_id=int(station_id),
            allow_when_only_playing=True,
        )

    counter = _count_music_since_last_jingle(conn, int(station_id))
    recent_jingle_ids = set(_recent_jingle_track_ids(conn, station_id, limit=1))
    recent_ad_ids = set(_recent_ad_track_ids(conn, station_id, limit=1))
    used_jingle_ids: set[int] = set()
    used_ad_ids: set[int] = set()
    inserted = 0

    for row in pending_rows:
        track_type = str(row["track_type"] or "music").strip().lower()
        if track_type == "music" and counter >= sweeper["interval"]:
            jingle = _pick_jingle(
                conn,
                int(station_id),
                sweeper["mode"],
                exclude_ids=recent_jingle_ids | used_jingle_ids,
            )
            if jingle:
                cur.execute(
                    "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (
                        int(station_id),
                        int(jingle["id"]),
                        next_pos,
                        f"jingle:{int(jingle['id'])}:{next_pos}",
                    ),
                )
                next_pos += 1
                inserted += 1
                used_jingle_ids.add(int(jingle["id"]))
                ad = _pick_ad(
                    conn,
                    int(station_id),
                    exclude_ids=recent_ad_ids | used_ad_ids,
                )
                if ad:
                    cur.execute(
                        "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                        "VALUES (?, ?, ?, 'pending', ?)",
                        (
                            int(station_id),
                            int(ad["id"]),
                            next_pos,
                            f"ad:{int(ad['id'])}:{next_pos}",
                        ),
                    )
                    next_pos += 1
                    inserted += 1
                    used_ad_ids.add(int(ad["id"]))
                counter = 0

        cur.execute("UPDATE queue_items SET position=? WHERE id=?", (next_pos, int(row["id"])))
        next_pos += 1

        if track_type == "jingle":
            counter = 0
        elif track_type == "music":
            counter += 1

    if removed > 0 or inserted > 0:
        conn.commit()
    return list(repo.list_active_ordered(station_id=station_id))


def ensure_broadcast_queue_filled(
    conn,
    station_id: int,
    refill_size: int = 10,
    exclude_ids: set[int] | None = None,
):
    return ensure_broadcast_queue_ready_for_playback(
        conn,
        station_id=station_id,
        refill_size=refill_size,
        allow_when_only_playing=False,
        exclude_ids=exclude_ids,
    )


def ensure_broadcast_queue_ready_for_playback(
    conn,
    station_id: int,
    refill_size: int = 10,
    allow_when_only_playing: bool = False,
    exclude_ids: set[int] | None = None,
):
    _purge_inactive_pending_queue_items(conn, int(station_id))
    repo = QueueRepository(conn)
    current = list(repo.list_active_ordered(station_id=station_id))
    has_pending = any(str(row["status"]) == "pending" for row in current)
    can_refill_while_playing = bool(current) and not has_pending and allow_when_only_playing
    if current and not can_refill_while_playing:
        return current

    with _QUEUE_AUTOFILL_LOCK:
        _purge_inactive_pending_queue_items(conn, int(station_id))
        current = list(repo.list_active_ordered(station_id=station_id))
        has_pending = any(str(row["status"]) == "pending" for row in current)
        can_refill_while_playing = bool(current) and not has_pending and allow_when_only_playing
        if current and not can_refill_while_playing:
            return current

        previous_track_id = _get_library_fallback_track_id(conn, int(station_id))
        selected_exclude_ids = {
            int(row["track_id"])
            for row in current
            if int(row["track_id"] or 0) > 0
        }
        if previous_track_id:
            selected_exclude_ids.add(int(previous_track_id))
        if exclude_ids:
            selected_exclude_ids.update(int(x) for x in exclude_ids if int(x) > 0)
        candidates = select_broadcast_queue_autofill_tracks(
            conn,
            station_id=station_id,
            limit=refill_size,
            exclude_ids=selected_exclude_ids,
        )

        # Interleave jingles based on sweeper settings
        sweeper = _get_sweeper_settings(conn, int(station_id))
        music_since = _count_music_since_last_jingle(conn, int(station_id))
        final_items = _interleave_with_jingles(
            conn, int(station_id), candidates, sweeper, music_since,
        )

        for index, item in enumerate(final_items, start=1):
            track_type = str(item.get("track_type", "music")).lower()
            dedupe_key = (
                f"jingle:{int(item['id'])}:{index}"
                if track_type == "jingle"
                else (
                    f"ad:{int(item['id'])}:{index}"
                    if track_type == "ad"
                    else f"autofill:{int(item['id'])}"
                )
            )
            repo.enqueue(
                station_id=int(station_id),
                track_id=int(item["id"]),
                dedupe_key=dedupe_key,
            )
        return list(repo.list_active_ordered(station_id=station_id))
