from fastapi.testclient import TestClient
from pathlib import Path

from app.main import app


def test_campaign_controls_are_self_contained_in_operator_ui():
    root = Path(__file__).resolve().parents[2] / "app" / "static" / "onair"
    html = (root / "index.html").read_text(encoding="utf-8")
    javascript = (root / "app.js").read_text(encoding="utf-8")
    for control_id in (
        "campaignForm",
        "campaignStartsAt",
        "campaignEndsAt",
        "campaignVotingEnabled",
        "campaignAiEnabled",
        "previewCampaignNamesButton",
        "applyCampaignNamesButton",
        "publishVotingRoundButton",
        "resolveVotingRoundButton",
        "watchdogState",
        "watchdogSummary",
        "refreshWatchdogButton",
        "repairWatchdogButton",
    ):
        assert f'id="{control_id}"' in html
    assert "/api/campaign/normalize-track-names" in javascript
    assert "/api/campaign/voting/round" in javascript
    assert "/api/campaign/voting/resolve" in javascript
    assert "/api/watchdog/status" in javascript
    assert "/api/watchdog/repair" in javascript
    assert "Publish next 3 queued songs" not in html


def test_legacy_ui_smoke_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    stations = c.get("/api/stations")
    assert stations.status_code == 200
    stations_payload = stations.json()
    assert isinstance(stations_payload.get("stations"), list)
    assert stations_payload["stations"]

    station_id = int(stations_payload["stations"][0]["id"])

    liquidsoap = c.get("/api/liquidsoap/status", params={"station_id": station_id})
    assert liquidsoap.status_code == 200
    assert "alive" in liquidsoap.json()

    ytdlp = c.get("/api/library/import/ytdlp/jobs/status", params={"limit_recent": 25})
    assert ytdlp.status_code == 200
    ytdlp_payload = ytdlp.json()
    assert isinstance(ytdlp_payload.get("queue"), list)
    assert isinstance(ytdlp_payload.get("recent"), list)
    assert isinstance(ytdlp_payload.get("counts"), dict)

    settings = c.get("/api/settings/station", params={"station_id": station_id})
    assert settings.status_code == 200
    settings_payload = settings.json()
    assert isinstance(settings_payload.get("settings"), dict)
    assert isinstance(settings_payload.get("station"), dict)

    tracks = c.get(
        "/api/tracks",
        params={"station_id": station_id, "page": 1, "per_page": 10},
    )
    assert tracks.status_code == 200
    tracks_payload = tracks.json()
    assert isinstance(tracks_payload.get("tracks"), list)

    playlists = c.get("/api/playlists", params={"station_id": station_id})
    assert playlists.status_code == 200
    assert isinstance(playlists.json(), list)

    queue = c.get("/api/queue", params={"station_id": station_id})
    assert queue.status_code == 200
    queue_payload = queue.json()
    assert isinstance(queue_payload.get("items"), list)

    program_queue = c.get("/api/program/queue", params={"station_id": station_id})
    assert program_queue.status_code == 200
    program_payload = program_queue.json()
    assert isinstance(program_payload.get("items"), list)
    assert "source" in program_payload
    assert "effective_source" in program_payload

    schedule = c.get("/api/schedule", params={"station_id": station_id})
    assert schedule.status_code == 200
    assert isinstance(schedule.json(), list)

    timeline = c.get("/api/schedule/timeline", params={"station_id": station_id})
    assert timeline.status_code == 200
    timeline_payload = timeline.json()
    assert isinstance(timeline_payload.get("items"), list)
    assert isinstance(timeline_payload.get("blocks"), list)

    logs = c.get(
        "/api/logs",
        params={"station_id": station_id, "scope": "play", "per_page": 25},
    )
    assert logs.status_code == 200
    logs_payload = logs.json()
    assert isinstance(logs_payload.get("logs"), list)

    ad_break_sets = c.get("/api/ad-break-sets", params={"station_id": station_id})
    assert ad_break_sets.status_code == 200
    breaks_payload = ad_break_sets.json()
    assert isinstance(breaks_payload.get("break_sets"), list)

    ad_campaigns = c.get("/api/ad-campaigns", params={"station_id": station_id})
    assert ad_campaigns.status_code == 200
    campaigns_payload = ad_campaigns.json()
    assert isinstance(campaigns_payload.get("campaigns"), list)

    ads_runtime = c.get("/api/ads/runtime", params={"station_id": station_id})
    assert ads_runtime.status_code == 200
    ads_runtime_payload = ads_runtime.json()
    assert isinstance(ads_runtime_payload.get("due_slots"), list)
    assert isinstance(ads_runtime_payload.get("next_slots"), list)
