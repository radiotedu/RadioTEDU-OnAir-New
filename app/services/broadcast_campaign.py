from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.repositories.queue_repo import QueueRepository
from app.services.track_naming import normalize_track_name


SOURCE_TYPE = "YouTube playlist"
ORIGINAL_METADATA_PREFIX = "RadioTEDU original source metadata: "
CAMPAIGN_STATIONS: dict[int, dict[str, str]] = {
    1: {
        "genre": "classical",
        "managed_folder": r"H:\RadioTEDU Songs\Classical",
        "previous_folder": r"H:\RadioTEDU Song Database Overflow\Classical",
        "previous_mode": "replace",
    },
    4: {
        "genre": "pop",
        "managed_folder": r"H:\RadioTEDU Songs\Pop",
        "previous_folder": r"H:\RadioTEDU Song Database Overflow\Pop\Events",
        "previous_mode": "merge",
    },
    8: {
        "genre": "rock",
        "managed_folder": r"H:\RadioTEDU Songs\Rock",
        "previous_folder": r"H:\RadioTEDU Song Database Overflow\Rock",
        "previous_mode": "replace",
    },
    9: {
        "genre": "energize",
        "managed_folder": r"H:\RadioTEDU Songs\Energize",
        "previous_folder": r"H:\RadioTEDU Song Database Overflow\Pop",
        "previous_mode": "replace",
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: object) -> datetime:
    token = str(value or "").strip()
    if token.endswith("Z"):
        token = f"{token[:-1]}+00:00"
    parsed = datetime.fromisoformat(token)
    if parsed.tzinfo is None:
        raise ValueError("campaign timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class BroadcastCampaignService:
    def __init__(self, conn):
        self.conn = conn

    def _latest_campaign(self):
        return self.conn.execute(
            "SELECT * FROM broadcast_campaigns ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def _active_campaign(self, now: datetime | None = None):
        instant = iso_timestamp(now or utc_now())
        return self.conn.execute(
            "SELECT * FROM broadcast_campaigns WHERE enabled=1 AND starts_at<=? AND ends_at>? "
            "ORDER BY id DESC LIMIT 1",
            (instant, instant),
        ).fetchone()

    def save_campaign(
        self,
        *,
        name: str,
        starts_at: object,
        ends_at: object,
        enabled: bool,
        voting_enabled: bool,
        ai_enabled: bool,
    ) -> dict:
        starts = parse_timestamp(starts_at)
        ends = parse_timestamp(ends_at)
        duration = ends - starts
        if duration < timedelta(hours=1) or duration > timedelta(days=62):
            raise ValueError("campaign duration must be between 1 hour and 62 days")
        safe_name = str(name or "RadioTEDU No-Copyright Month").strip()[:160]
        if not safe_name:
            raise ValueError("campaign name is required")
        available_station_ids = {
            int(row["id"])
            for row in self.conn.execute(
                "SELECT id FROM stations WHERE id IN (1,4,8,9)"
            ).fetchall()
        }
        missing_station_ids = sorted(set(CAMPAIGN_STATIONS) - available_station_ids)
        if missing_station_ids:
            missing = ",".join(str(item) for item in missing_station_ids)
            raise ValueError(f"campaign stations are missing: {missing}")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            latest = self._latest_campaign()
            if latest is None:
                cursor = self.conn.execute(
                    "INSERT INTO broadcast_campaigns "
                    "(name,enabled,starts_at,ends_at,source_type,voting_enabled,ai_enabled,restore_policy) "
                    "VALUES (?,?,?,?,?,?,?,'keep_campaign_library')",
                    (
                        safe_name,
                        int(bool(enabled)),
                        iso_timestamp(starts),
                        iso_timestamp(ends),
                        SOURCE_TYPE,
                        int(bool(voting_enabled)),
                        int(bool(ai_enabled)),
                    ),
                )
                campaign_id = int(cursor.lastrowid)
            else:
                campaign_id = int(latest["id"])
                self.conn.execute(
                    "UPDATE broadcast_campaigns SET name=?,enabled=?,starts_at=?,ends_at=?,"
                    "source_type=?,voting_enabled=?,ai_enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (
                        safe_name,
                        int(bool(enabled)),
                        iso_timestamp(starts),
                        iso_timestamp(ends),
                        SOURCE_TYPE,
                        int(bool(voting_enabled)),
                        int(bool(ai_enabled)),
                        campaign_id,
                    ),
                )

            for station_id, profile in CAMPAIGN_STATIONS.items():
                existing = self.conn.execute(
                    "SELECT previous_folder,previous_mode FROM broadcast_campaign_stations "
                    "WHERE campaign_id=? AND station_id=?",
                    (campaign_id, station_id),
                ).fetchone()
                current_settings = {
                    str(row["key"]): str(row["value"])
                    for row in self.conn.execute(
                        "SELECT key,value FROM station_settings WHERE station_id=? "
                        "AND key IN ('music_library_folder','library_management_mode')",
                        (station_id,),
                    ).fetchall()
                }
                current_folder = current_settings.get("music_library_folder", "")
                managed_folder = profile["managed_folder"]
                same_as_campaign = (
                    str(Path(current_folder)).casefold() == str(Path(managed_folder)).casefold()
                )
                previous_folder = (
                    str(existing["previous_folder"] or "")
                    if existing is not None
                    else (current_folder if current_folder and not same_as_campaign else profile["previous_folder"])
                )
                previous_mode = (
                    str(existing["previous_mode"] or "replace")
                    if existing is not None
                    else (
                        current_settings.get("library_management_mode", "replace")
                        if not same_as_campaign
                        else profile["previous_mode"]
                    )
                )
                self.conn.execute(
                    "INSERT INTO broadcast_campaign_stations "
                    "(campaign_id,station_id,genre,managed_folder,previous_folder,previous_mode) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(campaign_id,station_id) DO UPDATE SET "
                    "genre=excluded.genre,managed_folder=excluded.managed_folder",
                    (
                        campaign_id,
                        station_id,
                        profile["genre"],
                        managed_folder,
                        previous_folder,
                        previous_mode,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO station_settings (station_id,key,value,updated_at) "
                    "VALUES (?, 'campaign_ai_only', 'true', CURRENT_TIMESTAMP) "
                    "ON CONFLICT(station_id,key) DO UPDATE SET value='true',updated_at=CURRENT_TIMESTAMP",
                    (station_id,),
                )
                self.conn.execute(
                    "INSERT INTO station_settings (station_id,key,value,updated_at) "
                    "VALUES (?, 'ai_host_enabled', ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(station_id,key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (station_id, "true" if enabled and ai_enabled else "false"),
                )
            self._upsert_managed_profile_settings(campaign_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.status()

    def _upsert_managed_profile_settings(self, campaign_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT station_id,genre,managed_folder FROM broadcast_campaign_stations "
            "WHERE campaign_id=? ORDER BY station_id",
            (int(campaign_id),),
        ).fetchall()
        station_ids = []
        for row in rows:
            station_id = int(row["station_id"])
            station_ids.append(station_id)
            settings = {
                "music_library_folder": str(row["managed_folder"] or ""),
                "library_management_mode": "replace",
                "library_rescan_interval_seconds": "600",
                # Operators drop new audio into per-station Incoming
                # subfolders. Keep the campaign profile recursive so the
                # watchdog never undoes the UI's persisted live-folder choice.
                "library_recursive": "true",
                "library_skip_unplayable": "true",
                "library_profile_label": f"RadioTEDU {str(row['genre'] or '').strip()} playlist",
            }
            for key, value in settings.items():
                self.conn.execute(
                    "INSERT INTO station_settings (station_id,key,value,updated_at) "
                    "VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(station_id,key) DO UPDATE SET "
                    "value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (station_id, key, value),
                )
        return station_ids

    def ensure_managed_profiles(self) -> list[int]:
        active = self._active_campaign()
        if active is None:
            return []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            station_ids = self._upsert_managed_profile_settings(int(active["id"]))
            if set(station_ids) != set(CAMPAIGN_STATIONS):
                raise ValueError("campaign managed profiles are incomplete")
            self.conn.commit()
            return station_ids
        except Exception:
            self.conn.rollback()
            raise

    def status(self, now: datetime | None = None) -> dict:
        latest = self._latest_campaign()
        if latest is None:
            return {"configured": False, "active": False, "stations": [], "round": None}
        instant = now or utc_now()
        starts = parse_timestamp(latest["starts_at"])
        ends = parse_timestamp(latest["ends_at"])
        active = bool(latest["enabled"]) and starts <= instant < ends
        state = "active" if active else ("upcoming" if instant < starts else "expired")
        if not bool(latest["enabled"]):
            state = "disabled"
        station_rows = self.conn.execute(
            "SELECT cs.*,s.name AS station_name,COUNT(t.id) AS eligible_tracks "
            "FROM broadcast_campaign_stations cs JOIN stations s ON s.id=cs.station_id "
            "LEFT JOIN tracks t ON t.station_id=cs.station_id AND t.is_active=1 "
            "AND lower(t.track_type)='music' LEFT JOIN track_broadcast_metadata m "
            "ON m.track_id=t.id AND m.source_type=? WHERE cs.campaign_id=? "
            "GROUP BY cs.campaign_id,cs.station_id ORDER BY cs.station_id",
            (str(latest["source_type"]), int(latest["id"])),
        ).fetchall()
        # COUNT(t.id) above includes active music even when the left-joined
        # metadata row is absent; compute the authoritative eligible count in
        # a small station-scoped query instead.
        stations: list[dict] = []
        for row in station_rows:
            eligible = self.conn.execute(
                "SELECT COUNT(*) FROM tracks t JOIN track_broadcast_metadata m ON m.track_id=t.id "
                "WHERE t.station_id=? AND t.is_active=1 AND lower(t.track_type)='music' AND m.source_type=?",
                (int(row["station_id"]), str(latest["source_type"])),
            ).fetchone()[0]
            item = dict(row)
            item["eligible_tracks"] = int(eligible or 0)
            stations.append(item)
        return {
            "configured": True,
            "id": int(latest["id"]),
            "name": str(latest["name"]),
            "enabled": bool(latest["enabled"]),
            "active": active,
            "state": state,
            "starts_at": str(latest["starts_at"]),
            "ends_at": str(latest["ends_at"]),
            "source_type": str(latest["source_type"]),
            "voting_enabled": bool(latest["voting_enabled"]),
            "ai_enabled": bool(latest["ai_enabled"]),
            "restore_policy": str(latest["restore_policy"]),
            "stations": stations,
            "round": self.current_round(int(latest["id"]), now=instant),
        }

    def normalize_eligible_track_names(self, *, dry_run: bool = True) -> dict:
        campaign = self._latest_campaign()
        if campaign is None:
            raise ValueError("campaign is not configured")
        rows = self.conn.execute(
            "SELECT t.id,t.station_id,t.title,t.artist,m.version,m.notes FROM tracks t "
            "JOIN track_broadcast_metadata m ON m.track_id=t.id "
            "JOIN broadcast_campaign_stations cs ON cs.station_id=t.station_id AND cs.campaign_id=? "
            "WHERE t.is_active=1 AND lower(t.track_type)='music' AND m.source_type=? ORDER BY t.station_id,t.id",
            (int(campaign["id"]), str(campaign["source_type"])),
        ).fetchall()
        changed = 0
        samples: list[dict] = []
        if not dry_run:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                normalized = normalize_track_name(row["title"], row["artist"])
                is_changed = normalized.title != row["title"] or normalized.artist != row["artist"]
                changed += int(is_changed)
                if is_changed and len(samples) < 12:
                    samples.append(
                        {
                            "track_id": int(row["id"]),
                            "before": f"{row['artist']} - {row['title']}",
                            "after": normalized.label,
                        }
                    )
                if dry_run or not is_changed:
                    continue
                notes = str(row["notes"] or "")
                if ORIGINAL_METADATA_PREFIX not in notes:
                    snapshot = json.dumps(
                        {"title": str(row["title"] or ""), "artist": str(row["artist"] or "")},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    notes = f"{notes} | {ORIGINAL_METADATA_PREFIX}{snapshot}".strip(" |")
                self.conn.execute(
                    "UPDATE tracks SET title=?,artist=? WHERE id=?",
                    (normalized.title, normalized.artist, int(row["id"])),
                )
                self.conn.execute(
                    "UPDATE track_broadcast_metadata SET version=CASE WHEN trim(version)='' THEN ? ELSE version END, "
                    "notes=?,updated_at=CURRENT_TIMESTAMP WHERE track_id=?",
                    (normalized.version, notes, int(row["id"])),
                )
            if not dry_run:
                self.conn.execute(
                    "INSERT INTO operation_logs (level,event_type,message,payload_json) VALUES "
                    "('info','campaign_track_names_normalized','Campaign track names normalized',?)",
                    (json.dumps({"campaign_id": int(campaign["id"]), "changed": changed}),),
                )
                self.conn.commit()
        except Exception:
            if not dry_run:
                self.conn.rollback()
            raise
        return {"dry_run": bool(dry_run), "eligible": len(rows), "changed": changed, "samples": samples}

    def ai_track_allowed(self, *, station_id: int, track_id: int, now: datetime | None = None) -> bool:
        setting = self.conn.execute(
            "SELECT value FROM station_settings WHERE station_id=? AND key='campaign_ai_only'",
            (int(station_id),),
        ).fetchone()
        if setting is None or not _truthy(setting["value"]):
            return True
        campaign = self._active_campaign(now)
        if campaign is None or not bool(campaign["ai_enabled"]):
            return False
        member = self.conn.execute(
            "SELECT 1 FROM broadcast_campaign_stations WHERE campaign_id=? AND station_id=?",
            (int(campaign["id"]), int(station_id)),
        ).fetchone()
        if member is None:
            return False
        eligible = self.conn.execute(
            "SELECT 1 FROM tracks t JOIN track_broadcast_metadata m ON m.track_id=t.id "
            "WHERE t.id=? AND t.station_id=? AND t.is_active=1 AND lower(t.track_type)='music' "
            "AND m.source_type=?",
            (int(track_id), int(station_id), str(campaign["source_type"])),
        ).fetchone()
        return eligible is not None

    def create_round(self, *, duration_seconds: int = 45) -> dict:
        campaign = self._active_campaign()
        if campaign is None or not bool(campaign["voting_enabled"]):
            raise ValueError("an active voting campaign is required")
        duration = max(15, min(int(duration_seconds), 600))
        now = utc_now()
        now_text = iso_timestamp(now)
        self.conn.execute(
            "UPDATE genre_voting_rounds SET status='expired' "
            "WHERE campaign_id=? AND status='open' AND closes_at<=?",
            (int(campaign["id"]), now_text),
        )
        existing = self.conn.execute(
            "SELECT id FROM genre_voting_rounds WHERE campaign_id=? AND status='open' ORDER BY created_at DESC LIMIT 1",
            (int(campaign["id"]),),
        ).fetchone()
        if existing is not None:
            self.conn.commit()
            raise ValueError("a genre voting round is already open")
        genres = self.conn.execute(
            "SELECT genre FROM broadcast_campaign_stations WHERE campaign_id=? ORDER BY station_id",
            (int(campaign["id"]),),
        ).fetchall()
        if len(genres) < 2:
            raise ValueError("campaign requires at least two genres")
        round_id = f"genre-{secrets.token_hex(12)}"
        self.conn.execute(
            "INSERT INTO genre_voting_rounds (id,campaign_id,status,opens_at,closes_at) VALUES (?,?,'open',?,?)",
            (round_id, int(campaign["id"]), now_text, iso_timestamp(now + timedelta(seconds=duration))),
        )
        self.conn.commit()
        return self.current_round(int(campaign["id"])) or {}

    def current_round(self, campaign_id: int, *, now: datetime | None = None) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM genre_voting_rounds WHERE campaign_id=? ORDER BY created_at DESC LIMIT 1",
            (int(campaign_id),),
        ).fetchone()
        if row is None:
            return None
        counts = {
            str(item["genre"]): int(item["votes"] or 0)
            for item in self.conn.execute(
                "SELECT genre,COUNT(*) AS votes FROM genre_votes WHERE round_id=? GROUP BY genre",
                (str(row["id"]),),
            ).fetchall()
        }
        genres = [
            str(item["genre"])
            for item in self.conn.execute(
                "SELECT genre FROM broadcast_campaign_stations WHERE campaign_id=? ORDER BY station_id",
                (int(campaign_id),),
            ).fetchall()
        ]
        status = str(row["status"])
        closes = parse_timestamp(row["closes_at"])
        if status == "open" and (now or utc_now()) >= closes:
            status = "ready_to_resolve"
        return {
            "id": str(row["id"]),
            "status": status,
            "opens_at": str(row["opens_at"]),
            "closes_at": str(row["closes_at"]),
            "winning_genre": str(row["winning_genre"] or ""),
            "queued_track_id": row["queued_track_id"],
            "genres": [{"genre": genre, "votes": counts.get(genre, 0)} for genre in genres],
            "total_votes": sum(counts.values()),
        }

    def record_vote(self, *, genre: str, voter_hash: str) -> dict:
        campaign = self._active_campaign()
        if campaign is None or not bool(campaign["voting_enabled"]):
            raise ValueError("genre voting is not active")
        current = self.current_round(int(campaign["id"]))
        if current is None or current["status"] != "open":
            raise ValueError("there is no open genre voting round")
        safe_genre = str(genre or "").strip().lower()
        if safe_genre not in {item["genre"] for item in current["genres"]}:
            raise ValueError("genre is not eligible for this campaign")
        try:
            self.conn.execute(
                "INSERT INTO genre_votes (round_id,genre,voter_hash) VALUES (?,?,?)",
                (str(current["id"]), safe_genre, str(voter_hash)),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValueError("this voter already voted in the round") from exc
        return self.current_round(int(campaign["id"])) or {}

    def resolve_round(self, *, force: bool = False) -> dict:
        campaign = self._active_campaign()
        if campaign is None or not bool(campaign["voting_enabled"]):
            raise ValueError("genre voting is not active")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM genre_voting_rounds WHERE campaign_id=? AND status='open' "
                "ORDER BY created_at DESC LIMIT 1",
                (int(campaign["id"]),),
            ).fetchone()
            if row is None:
                raise ValueError("there is no open genre voting round")
            if not force and utc_now() < parse_timestamp(row["closes_at"]):
                raise ValueError("genre voting round is still open")
            station_rows = self.conn.execute(
                "SELECT cs.station_id,cs.genre,cs.last_selected_at,COUNT(v.id) AS votes "
                "FROM broadcast_campaign_stations cs LEFT JOIN genre_votes v "
                "ON v.round_id=? AND v.genre=cs.genre WHERE cs.campaign_id=? "
                "GROUP BY cs.station_id,cs.genre ORDER BY votes DESC,COALESCE(cs.last_selected_at,''),cs.station_id",
                (str(row["id"]), int(campaign["id"])),
            ).fetchall()
            if not station_rows:
                raise ValueError("campaign has no genre stations")
            max_votes = max(int(item["votes"] or 0) for item in station_rows)
            winner = next(item for item in station_rows if int(item["votes"] or 0) == max_votes)
            track = self.conn.execute(
                "SELECT t.id,t.title,t.artist FROM tracks t JOIN track_broadcast_metadata m ON m.track_id=t.id "
                "WHERE t.station_id=? AND t.is_active=1 AND lower(t.track_type)='music' AND m.source_type=? "
                "AND NOT EXISTS (SELECT 1 FROM queue_items q WHERE q.track_id=t.id AND q.status IN ('pending','playing')) "
                "ORDER BY t.play_count ASC,COALESCE(t.last_played_at,''),t.id LIMIT 1",
                (int(winner["station_id"]), str(campaign["source_type"])),
            ).fetchone()
            if track is None:
                track = self.conn.execute(
                    "SELECT t.id,t.title,t.artist FROM tracks t JOIN track_broadcast_metadata m ON m.track_id=t.id "
                    "WHERE t.station_id=? AND t.is_active=1 AND lower(t.track_type)='music' AND m.source_type=? "
                    "ORDER BY t.play_count ASC,COALESCE(t.last_played_at,''),t.id LIMIT 1",
                    (int(winner["station_id"]), str(campaign["source_type"])),
                ).fetchone()
            if track is None:
                raise ValueError("winning genre has no eligible tracks")
            queue_id, created = QueueRepository(self.conn).enqueue_or_get_existing(
                int(winner["station_id"]),
                int(track["id"]),
                f"campaign-genre-round:{row['id']}",
                manage_transaction=False,
            )
            resolved_at = iso_timestamp(utc_now())
            self.conn.execute(
                "UPDATE genre_voting_rounds SET status='resolved',winning_genre=?,queued_track_id=?,resolved_at=? WHERE id=?",
                (str(winner["genre"]), int(track["id"]), resolved_at, str(row["id"])),
            )
            self.conn.execute(
                "UPDATE broadcast_campaign_stations SET last_selected_at=? WHERE campaign_id=? AND station_id=?",
                (resolved_at, int(campaign["id"]), int(winner["station_id"])),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            "round_id": str(row["id"]),
            "winning_genre": str(winner["genre"]),
            "station_id": int(winner["station_id"]),
            "track_id": int(track["id"]),
            "track_title": str(track["title"]),
            "track_artist": str(track["artist"]),
            "queue_item_id": int(queue_id),
            "queued": bool(created),
        }
