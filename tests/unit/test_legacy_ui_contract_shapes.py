from fastapi.testclient import TestClient

from app.main import app


def test_playlist_and_ads_contract_shapes_for_legacy_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    tr = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Contract Song",
            "artist": "Contract Artist",
            "track_type": "music",
            "duration": 120.0,
            "file_path": "C:/contract/song.mp3",
        },
    )
    assert tr.status_code == 200
    track_id = int(tr.json()["track_id"])

    pl = c.post(
        "/api/playlists",
        json={"station_id": 1, "name": "Contract Playlist", "description": "UI"},
    )
    assert pl.status_code == 200
    playlist_id = int(pl.json()["playlist_id"])
    add = c.post(f"/api/playlists/{playlist_id}/items", json={"track_id": track_id})
    assert add.status_code == 200

    playlists = c.get("/api/playlists", params={"station_id": 1})
    assert playlists.status_code == 200
    row = next((x for x in playlists.json() if int(x["id"]) == playlist_id), None)
    assert row is not None
    assert "description" in row
    assert "playlist_type" in row
    assert "item_count" in row

    detail = c.get(f"/api/playlists/{playlist_id}")
    assert detail.status_code == 200
    item = (detail.json().get("items") or [None])[0]
    assert item is not None
    assert "item_id" in item
    assert "title" in item
    assert "artist" in item
    assert "duration" in item

    b = c.post(
        "/api/ad-break-sets",
        json={
            "station_id": 1,
            "name": "Contract Break",
            "is_active": True,
            "slots": [{"slot_time": "12:00", "day_of_week": "*"}],
        },
    )
    assert b.status_code == 200

    campaign = c.post(
        "/api/ad-campaigns",
        json={
            "station_id": 1,
            "name": "Contract Campaign",
            "is_active": True,
            "slot_ids": [1],
            "track_ids": [track_id],
        },
    )
    assert campaign.status_code == 200

    break_sets = c.get("/api/ad-break-sets", params={"station_id": 1})
    assert break_sets.status_code == 200
    break_data = break_sets.json()
    assert isinstance(break_data.get("break_sets"), list)

    campaigns = c.get("/api/ad-campaigns", params={"station_id": 1})
    assert campaigns.status_code == 200
    campaign_data = campaigns.json()
    assert isinstance(campaign_data.get("campaigns"), list)

    runtime = c.get("/api/ads/runtime", params={"station_id": 1})
    assert runtime.status_code == 200
    runtime_data = runtime.json()
    assert isinstance(runtime_data.get("due_slots"), list)
    assert isinstance(runtime_data.get("next_slots"), list)
    assert isinstance(runtime_data.get("history"), list)
