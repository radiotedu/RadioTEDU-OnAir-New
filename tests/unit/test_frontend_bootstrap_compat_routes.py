from fastapi.testclient import TestClient

from app.main import app


def test_frontend_bootstrap_routes_exist_and_match_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setattr("app.main.bootstrap_dependencies", lambda: {})
    monkeypatch.setattr("app.main._autostart_station_worker_loops", lambda conn: None)
    c = TestClient(app)
    route_paths = {getattr(route, "path", "") for route in app.routes}

    created = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Bootstrap Song",
            "artist": "Tester",
            "file_path": "C:/media/music/bootstrap-song.mp3",
            "track_type": "music",
            "genre": "Pop",
            "language": "TR",
        },
    )
    assert created.status_code == 200

    tracks = c.get("/api/tracks", params={"station_id": 1, "page": 1, "per_page": 20})
    assert tracks.status_code == 200
    tracks_payload = tracks.json()
    assert isinstance(tracks_payload.get("tracks"), list)
    assert isinstance(tracks_payload.get("items"), list)
    assert int(tracks_payload.get("page", 0)) == 1

    filters = c.get(
        "/api/tracks/filters/options",
        params={"station_id": 1, "library_scope": "local"},
    )
    assert filters.status_code == 200
    assert isinstance(filters.json().get("artists"), list)

    next_track = c.get("/api/tracks/next", params={"station_id": 1})
    assert next_track.status_code == 200

    rules = c.get(
        "/api/library/metadata/rules",
        params={"station_id": 1, "include_inactive": True},
    )
    assert rules.status_code == 200
    assert isinstance(rules.json().get("rules"), list)

    created_rule = c.post(
        "/api/library/metadata/rules",
        json={
            "station_id": 1,
            "scope": "station",
            "name": "title clean",
            "target_field": "title",
            "match_type": "contains",
            "pattern": "demo",
            "replacement": "",
            "is_case_sensitive": False,
            "priority": 100,
            "is_active": True,
        },
    )
    assert created_rule.status_code == 200
    rule_id = int(created_rule.json()["id"])
    assert rule_id > 0

    toggled = c.put(f"/api/library/metadata/rules/{rule_id}", json={"is_active": False})
    assert toggled.status_code == 200

    sweeper = c.get("/api/sweeper/config", params={"station_id": 1})
    assert sweeper.status_code == 200
    assert "enabled" in sweeper.json()

    sweeper_save = c.post(
        "/api/sweeper/config",
        json={"station_id": 1, "enabled": True, "interval": 4, "mode": "random"},
    )
    assert sweeper_save.status_code == 200

    assert "/api/library/metadata/autofix" in route_paths
    assert "/api/library/bpm/analyze" in route_paths

    scan = c.post(
        "/api/scanner/scan",
        params={"station_id": 1, "trim_silence": True, "clean_intro": True},
    )
    assert scan.status_code == 200
    assert "results" in scan.json()

    assert "/api/library/import/upload" in route_paths

    media = c.get("/api/media/not-found.mp3")
    assert media.status_code == 404
