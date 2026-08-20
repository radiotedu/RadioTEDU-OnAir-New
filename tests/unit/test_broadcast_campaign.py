from __future__ import annotations

from datetime import timedelta

from app.db import get_connection, init_db
from app.repositories.queue_repo import QueueRepository
from app.services.ai_prefetch import AIPrefetchService
from app.services.broadcast_campaign import (
    BroadcastCampaignService,
    ORIGINAL_METADATA_PREFIX,
    iso_timestamp,
    utc_now,
)


def _track(conn, station_id: int, title: str, artist: str, source_type: str) -> int:
    cursor = conn.execute(
        "INSERT INTO tracks (station_id,title,artist,track_type,file_path,is_active,duration) "
        "VALUES (?,?,?,'music',?,1,180)",
        (station_id, title, artist, f"C:/campaign/{station_id}/{title}.mp3"),
    )
    track_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO track_broadcast_metadata (track_id,source_type,source_reference,notes) "
        "VALUES (?,?,?,'original-note')",
        (track_id, source_type, f"https://example.test/{track_id}"),
    )
    conn.commit()
    return track_id


def _ensure_campaign_stations(conn) -> None:
    for station_id, name in ((1, "Classical"), (4, "Pop"), (8, "Rock"), (9, "Energize")):
        conn.execute("INSERT OR IGNORE INTO stations (id,name) VALUES (?,?)", (station_id, name))
    conn.commit()


def _active_payload() -> dict:
    now = utc_now()
    return {
        "name": "No-Copyright Month",
        "starts_at": iso_timestamp(now - timedelta(minutes=1)),
        "ends_at": iso_timestamp(now + timedelta(days=30)),
        "enabled": True,
        "voting_enabled": True,
        "ai_enabled": True,
    }


def test_campaign_eligibility_and_ai_gate(tmp_path):
    init_db()
    conn = get_connection()
    _ensure_campaign_stations(conn)
    eligible = _track(
        conn,
        1,
        '📜 Copyright Free Classical Music - "Ephemera" by Scott Buckley 🇦🇺',
        "BreakingCopyright — Royalty Free Music",
        "YouTube playlist",
    )
    ineligible = _track(conn, 1, "Licensed Song", "Label Artist", "Purchased download")
    service = BroadcastCampaignService(conn)
    status = service.save_campaign(**_active_payload())
    assert status["active"] is True
    managed_settings = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            "SELECT key,value FROM station_settings WHERE station_id=1 AND key IN "
            "('music_library_folder','library_management_mode','library_rescan_interval_seconds',"
            "'library_recursive','library_skip_unplayable')"
        ).fetchall()
    }
    assert managed_settings == {
        "music_library_folder": r"H:\RadioTEDU Songs\Classical",
        "library_management_mode": "replace",
        "library_rescan_interval_seconds": "600",
        "library_recursive": "true",
        "library_skip_unplayable": "true",
    }
    assert service.ai_track_allowed(station_id=1, track_id=eligible) is True
    assert service.ai_track_allowed(station_id=1, track_id=ineligible) is False
    conn.close()


def test_disabled_campaign_does_not_rewrite_operator_library_folder(tmp_path):
    init_db()
    conn = get_connection()
    _ensure_campaign_stations(conn)
    service = BroadcastCampaignService(conn)
    payload = _active_payload()
    payload.update(enabled=False, voting_enabled=False, ai_enabled=False)
    service.save_campaign(**payload)
    conn.execute(
        "UPDATE station_settings SET value=? WHERE station_id=1 AND key='music_library_folder'",
        (r"H:\RadioTEDU Songs\Classical",),
    )
    conn.commit()

    assert service.ensure_managed_profiles() == []
    folder = conn.execute(
        "SELECT value FROM station_settings WHERE station_id=1 AND key='music_library_folder'"
    ).fetchone()["value"]
    assert folder == r"H:\RadioTEDU Songs\Classical"
    conn.close()


def test_normalization_preserves_original_source_metadata(tmp_path):
    init_db()
    conn = get_connection()
    _ensure_campaign_stations(conn)
    track_id = _track(
        conn,
        8,
        "Rock Sport Racing by Infraction [No Copyright Music] / I Will Run",
        "Infraction - No Copyright Music",
        "YouTube playlist",
    )
    service = BroadcastCampaignService(conn)
    service.save_campaign(**_active_payload())
    preview = service.normalize_eligible_track_names(dry_run=True)
    assert preview["changed"] == 1
    applied = service.normalize_eligible_track_names(dry_run=False)
    assert applied["changed"] == 1
    row = conn.execute(
        "SELECT t.title,t.artist,m.notes FROM tracks t JOIN track_broadcast_metadata m ON m.track_id=t.id WHERE t.id=?",
        (track_id,),
    ).fetchone()
    assert (row["artist"], row["title"]) == ("Infraction", "I Will Run")
    assert "original-note" in row["notes"]
    assert ORIGINAL_METADATA_PREFIX in row["notes"]
    assert "Rock Sport Racing" in row["notes"]
    assert service.normalize_eligible_track_names(dry_run=False)["changed"] == 0
    conn.close()


def test_genre_vote_resolves_only_to_eligible_track(client):
    conn = get_connection()
    _ensure_campaign_stations(conn)
    rock_track = _track(conn, 8, "Infraction - I Will Run", "Infraction", "YouTube playlist")
    _track(conn, 8, "Licensed Rock", "Other", "Purchased download")
    conn.close()
    saved = client.put("/api/campaign", json=_active_payload())
    assert saved.status_code == 200, saved.text
    opened = client.post("/api/campaign/voting/round", json={"duration_seconds": 45})
    assert opened.status_code == 200, opened.text
    vote = client.post(
        "/api/public/campaign/vote",
        json={"genre": "rock", "voter_id": "browser-session-0001"},
        headers={"X-Test-No-Auto-Auth": "1", "User-Agent": "campaign-test"},
    )
    assert vote.status_code == 200, vote.text
    duplicate = client.post(
        "/api/public/campaign/vote",
        json={"genre": "rock", "voter_id": "browser-session-0001"},
        headers={"X-Test-No-Auto-Auth": "1", "User-Agent": "campaign-test"},
    )
    assert duplicate.status_code == 409
    resolved = client.post("/api/campaign/voting/resolve", json={"force": True})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["winning_genre"] == "rock"
    assert resolved.json()["track_id"] == rock_track

    public = client.get(
        "/api/public/campaign",
        headers={"X-Test-No-Auto-Auth": "1"},
    )
    assert public.status_code == 200
    body = public.json()
    assert body["round"]["winning_genre"] == "rock"
    assert all("managed_folder" not in item for item in body["genres"])


def test_ai_prefetch_excludes_non_campaign_music(tmp_path):
    init_db()
    conn = get_connection()
    _ensure_campaign_stations(conn)
    eligible = _track(conn, 1, "Eligible", "Artist", "YouTube playlist")
    ineligible = _track(conn, 1, "Ineligible", "Artist", "Purchased download")
    BroadcastCampaignService(conn).save_campaign(**_active_payload())
    QueueRepository(conn).enqueue(1, eligible)
    QueueRepository(conn).enqueue(1, ineligible)
    conn.close()
    items = AIPrefetchService._get_upcoming_items(1, 10)
    assert [int(item["track_id"]) for item in items] == [eligible]
