from fastapi.testclient import TestClient

from app.main import app


def test_legacy_ads_break_set_and_campaign_endpoints():
    c = TestClient(app)
    track = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Ad Track",
            "artist": "Brand",
            "track_type": "ads",
            "duration": 15.0,
            "file_path": "C:/media/ad-track.mp3",
        },
    )
    assert track.status_code == 200
    track_id = int(track.json()["track_id"])

    b = c.post(
        "/api/ad-break-sets",
        json={
            "station_id": 1,
            "name": "Top of Hour",
            "description": "Hourly slots",
            "is_active": True,
            "slots": [
                {"slot_time": "09:00", "day_of_week": "*", "position": 0, "is_active": True},
                {"slot_time": "15:30", "day_of_week": "*", "position": 1, "is_active": True},
            ],
        },
    )
    assert b.status_code == 200
    break_set_id = int(b.json()["break_set_id"])

    x = c.get("/api/ad-break-sets", params={"station_id": 1})
    assert x.status_code == 200
    data = x.json()
    assert int(data["station_id"]) == 1
    assert isinstance(data.get("break_sets"), list)
    row = next((item for item in data["break_sets"] if int(item["id"]) == break_set_id), None)
    assert row is not None
    assert row["description"] == "Hourly slots"
    assert row["is_active"] is True
    assert len(row.get("slots") or []) == 2

    campaign = c.post(
        "/api/ad-campaigns",
        json={
            "station_id": 1,
            "name": "Prime Campaign",
            "is_active": True,
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
            "day_interval": 1,
            "daily_repeat_limit": 5,
            "priority": 10,
            "notes": "Test campaign",
            "slot_ids": [1, 2],
            "track_ids": [track_id],
        },
    )
    assert campaign.status_code == 200
    campaign_id = int(campaign.json()["campaign_id"])

    listed_campaigns = c.get("/api/ad-campaigns", params={"station_id": 1})
    assert listed_campaigns.status_code == 200
    campaigns_data = listed_campaigns.json()
    assert int(campaigns_data["station_id"]) == 1
    assert isinstance(campaigns_data.get("campaigns"), list)
    cmp_row = next(
        (item for item in campaigns_data["campaigns"] if int(item["id"]) == campaign_id),
        None,
    )
    assert cmp_row is not None
    assert cmp_row["is_active"] is True
    assert int(cmp_row["daily_repeat_limit"]) == 5
    assert isinstance(cmp_row.get("tracks"), list)
    assert cmp_row["tracks"] and int(cmp_row["tracks"][0]["track_id"]) == track_id

    runtime = c.get("/api/ads/runtime", params={"station_id": 1})
    assert runtime.status_code == 200
    runtime_data = runtime.json()
    assert "due_slots" in runtime_data
    assert "next_slots" in runtime_data
    assert isinstance(runtime_data.get("history"), list)


def test_ad_delete_rejects_wrong_station_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)
    stations = c.get("/api/stations")
    assert stations.status_code == 200
    created_station = c.post("/api/stations", json={"name": "Station Two"})
    assert created_station.status_code == 200

    track = c.post(
        "/api/tracks",
        json={
            "station_id": 2,
            "title": "Station Two Ad",
            "artist": "Brand",
            "track_type": "ads",
            "duration": 15.0,
            "file_path": "C:/media/station-two-ad.mp3",
        },
    )
    assert track.status_code == 200
    track_id = int(track.json()["track_id"])

    break_set = c.post(
        "/api/ad-break-sets",
        json={
            "station_id": 2,
            "name": "Station Two Break",
            "is_active": True,
            "slots": [{"slot_time": "09:00", "day_of_week": "*", "position": 0}],
        },
    )
    assert break_set.status_code == 200
    break_set_id = int(break_set.json()["break_set_id"])

    campaign = c.post(
        "/api/ad-campaigns",
        json={
            "station_id": 2,
            "name": "Station Two Campaign",
            "is_active": True,
            "slot_ids": [1],
            "track_ids": [track_id],
        },
    )
    assert campaign.status_code == 200
    campaign_id = int(campaign.json()["campaign_id"])

    wrong_break_delete = c.delete(
        f"/api/ad-break-sets/{break_set_id}",
        params={"station_id": 1},
    )
    wrong_campaign_delete = c.delete(
        f"/api/ad-campaigns/{campaign_id}",
        params={"station_id": 1},
    )
    assert wrong_break_delete.status_code == 404
    assert wrong_campaign_delete.status_code == 404

    listed_breaks = c.get("/api/ad-break-sets", params={"station_id": 2})
    listed_campaigns = c.get("/api/ad-campaigns", params={"station_id": 2})
    assert any(int(item["id"]) == break_set_id for item in listed_breaks.json()["break_sets"])
    assert any(int(item["id"]) == campaign_id for item in listed_campaigns.json()["campaigns"])
