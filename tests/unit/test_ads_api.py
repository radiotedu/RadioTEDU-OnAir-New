import sqlite3

from fastapi.testclient import TestClient

from app.api import legacy as legacy_api
from app.api import ads as ads_api
from app.db import get_connection, init_db
from app.main import app


def test_ads_items_create_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (6, 'Ads Test')")
    cur.execute(
        "INSERT INTO tracks (id, station_id, title, artist, track_type, musicbrainz_recordingid, file_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (601, 6, "AdTrack", "Brand", "ad", "", "C:/music/ad-track.mp3"),
    )
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value) VALUES (6, 'hourly_ad_enabled', 'true') "
        "ON CONFLICT(station_id, key) DO UPDATE SET value='true'"
    )
    conn.commit()

    client = TestClient(app)
    create_res = client.post(
        "/api/ads/items",
        json={
            "station_id": 6,
            "track_id": 601,
            "due_at": "2000-01-01 00:00:00",
            "priority": 3,
        },
    )
    assert create_res.status_code == 200
    assert create_res.json()["ok"] is True

    list_res = client.get("/api/ads/items", params={"station_id": 6, "limit": 10})
    assert list_res.status_code == 200
    payload = list_res.json()
    assert payload["station_id"] == 6
    assert payload["items"]
    assert int(payload["items"][0]["track_id"]) == 601

    break_res = client.post(
        "/api/ad-break-sets",
        json={
            "station_id": 6,
            "name": "Ads Console Break",
            "is_active": True,
            "slots": [{"slot_time": "23:59", "day_of_week": "*", "position": 0}],
        },
    )
    assert break_res.status_code == 200
    break_set_id = int(break_res.json()["break_set_id"])
    campaign_res = client.post(
        "/api/ad-campaigns",
        json={
            "station_id": 6,
            "name": "Ads Console Campaign",
            "is_active": True,
            "slot_ids": [break_set_id],
            "track_ids": [601],
        },
    )
    assert campaign_res.status_code == 200
    campaign_id = int(campaign_res.json()["campaign_id"])

    console_res = client.get("/api/ads/console", params={"station_id": 6, "limit": 50})
    assert console_res.status_code == 200
    console = console_res.json()
    assert console["station_id"] == 6
    assert console["items"]["station_id"] == 6
    assert any(int(item["track_id"]) == 601 for item in console["items"]["items"])
    assert any(int(item["id"]) == break_set_id for item in console["break_sets"]["break_sets"])
    assert any(int(item["id"]) == campaign_id for item in console["campaigns"]["campaigns"])
    assert console["runtime"]["break_set_count"] == 1
    assert console["runtime"]["campaign_count"] == 1
    assert console["runtime"]["station_id"] == 6


def test_ads_console_retries_transient_sqlite_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    original_get_read_connection = legacy_api.get_read_connection
    attempts = 0

    def locked_once(*, timeout_seconds=3.0):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_get_read_connection(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(legacy_api, "get_read_connection", locked_once)
    response = TestClient(app).get(
        "/api/ads/console", params={"station_id": 6006, "limit": 50}
    )

    assert response.status_code == 200
    assert response.json()["station_id"] == 6006
    assert attempts == 2


def test_ads_console_returns_retryable_status_after_persistent_sqlite_lock(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    attempts = 0

    def always_locked(*, timeout_seconds=3.0):
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(legacy_api, "get_read_connection", always_locked)
    response = TestClient(app).get(
        "/api/ads/console", params={"station_id": 6007, "limit": 50}
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert attempts == 3


def test_ads_catalog_sync_is_station_scoped_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    conn.executemany(
        "INSERT OR IGNORE INTO stations (id, name) VALUES (?, ?)",
        [(6, "Ads Sync A"), (7, "Ads Sync B")],
    )
    conn.commit()

    ads_root = tmp_path / "approved-ads"
    ads_root.mkdir()
    assets = []
    for name in ("Campus.mp3", "Student Clubs.mp3"):
        path = ads_root / name
        path.write_bytes(b"approved audio")
        stat = path.stat()
        assets.append(
            {
                "relative_path": name,
                "file_name": name,
                "title": path.stem,
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "generation": 3,
                "stale": False,
                "path": str(path.resolve()),
            }
        )

    class FakeCatalog:
        def list_items(self, product, *, limit=500):
            assert product == "ads"
            assert limit == 500
            return {"product": "ads", "generation": 3, "items": assets}

    monkeypatch.setattr(ads_api, "get_product_media_catalog_service", lambda: FakeCatalog())
    probe_attempts = {}

    def probe_with_one_transient_failure(path, *, timeout_seconds):
        assert timeout_seconds == 10.0
        probe_attempts[path] = probe_attempts.get(path, 0) + 1
        if probe_attempts[path] == 1:
            return 0.0
        return 18.25

    monkeypatch.setattr(ads_api, "probe_duration", probe_with_one_transient_failure)
    client = TestClient(app)

    response = client.post(
        "/api/ads/catalog/sync",
        json={"station_ids": [6, 7]},
    )
    assert response.status_code == 200
    assert response.json()["created"] == 4
    assert response.json()["asset_count"] == 2
    assert len(probe_attempts) == 2
    assert set(probe_attempts.values()) == {2}

    conn = get_connection()
    rows = conn.execute(
        "SELECT station_id, title, duration, track_type, exclude_from_autoplay "
        "FROM tracks WHERE station_id IN (6, 7) AND track_type='ad' ORDER BY station_id, title"
    ).fetchall()
    assert len(rows) == 4
    assert {row["station_id"] for row in rows} == {6, 7}
    assert {row["duration"] for row in rows} == {18.25}
    assert all(row["exclude_from_autoplay"] == 1 for row in rows)

    catalog_response = client.get("/api/ads/catalog", params={"station_id": 6})
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    assert len(catalog["catalog_items"]) == 2
    assert len(catalog["tracks"]) == 2
    assert all("path" not in item for item in catalog["catalog_items"])
    assert all("6" in item["track_ids_by_station"] for item in catalog["catalog_items"])

    repeat = client.post("/api/ads/catalog/sync", json={"station_ids": [6, 7]})
    assert repeat.status_code == 200
    assert repeat.json()["created"] == 0
    assert repeat.json()["reused"] == 4


def test_ads_catalog_read_returns_retryable_status_when_database_is_locked(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    class EmptyCatalog:
        def list_items(self, product, *, limit=500):
            assert product == "ads"
            assert limit == 500
            return {"product": "ads", "generation": 1, "items": []}

    def locked_read(*, timeout_seconds=1.5):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ads_api, "get_product_media_catalog_service", lambda: EmptyCatalog())
    monkeypatch.setattr(ads_api, "get_read_connection", locked_read)

    response = TestClient(app).get("/api/ads/catalog", params={"station_id": 6})

    assert response.status_code == 503
    assert response.json()["detail"] == "ads_catalog_busy"
    assert response.headers["retry-after"] == "1"
