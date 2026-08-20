from fastapi.testclient import TestClient

from app.main import app


def test_legacy_playlist_crud_roundtrip():
    c = TestClient(app)
    created = c.post(
        "/api/playlists",
        json={"station_id": 1, "name": "Drive Time", "description": "Evening set"},
    )
    assert created.status_code == 200
    playlist_id = created.json()["id"]
    assert int(created.json()["playlist_id"]) == int(playlist_id)
    add_item = c.post(f"/api/playlists/{playlist_id}/items", json={"track_id": 10})
    assert add_item.status_code == 200
    assert int(add_item.json()["item_id"]) > 0
    listed = c.get("/api/playlists", params={"station_id": 1})
    assert listed.status_code == 200
    match = next((p for p in listed.json() if int(p["id"]) == int(playlist_id)), None)
    assert match is not None
    assert str(match["description"]) == "Evening set"
    assert str(match["playlist_type"]) == "manual"
    assert int(match["item_count"]) == 1


def test_auto_generate_playlist_creates_filtered_items(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    t1 = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "B Song",
            "artist": "Artist One",
            "genre": "Pop",
            "bpm": 110,
            "track_type": "music",
            "file_path": "C:/music/b-song.mp3",
        },
    )
    assert t1.status_code == 200
    id1 = int(t1.json()["track_id"])

    t2 = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "A Song",
            "artist": "Artist One",
            "genre": "Pop",
            "bpm": 118,
            "track_type": "music",
            "file_path": "C:/music/a-song.mp3",
        },
    )
    assert t2.status_code == 200
    id2 = int(t2.json()["track_id"])

    c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Ad Item",
            "artist": "Brand",
            "genre": "Promo",
            "bpm": 95,
            "track_type": "ads",
            "file_path": "C:/music/ad-item.mp3",
        },
    )

    created = c.post(
        "/api/playlists/auto/generate",
        json={
            "name": "Pop AtoZ",
            "description": "generated",
            "station_id": 1,
            "artist": "Artist One",
            "genre": "Pop",
            "track_type": "music",
            "bpm_min": 100,
            "bpm_max": 130,
            "limit": 50,
            "sort_by": "title",
        },
    )
    assert created.status_code == 200
    payload = created.json()
    assert int(payload["track_count"]) == 2
    playlist_id = int(payload["playlist_id"])

    detail = c.get(f"/api/playlists/{playlist_id}")
    assert detail.status_code == 200
    items = detail.json().get("items") or []
    assert [int(item["track_id"]) for item in items] == [id2, id1]
    assert all(int(item["item_id"]) > 0 for item in items)
    assert all("title" in item and "artist" in item and "duration" in item for item in items)


def test_auto_generate_playlist_maps_ad_track_type_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    created_track = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Ad Item",
            "artist": "Brand",
            "genre": "Promo",
            "bpm": 95,
            "track_type": "ads",
            "file_path": "C:/music/ad-item.mp3",
        },
    )
    assert created_track.status_code == 200
    track_id = int(created_track.json()["track_id"])

    created = c.post(
        "/api/playlists/auto/generate",
        json={
            "name": "Ad Playlist",
            "station_id": 1,
            "track_type": "ad",
            "limit": 10,
            "sort_by": "random",
        },
    )
    assert created.status_code == 200
    data = created.json()
    assert int(data["track_count"]) == 1

    playlist_id = int(data["playlist_id"])
    detail = c.get(f"/api/playlists/{playlist_id}")
    assert detail.status_code == 200
    items = detail.json().get("items") or []
    assert len(items) == 1
    assert int(items[0]["track_id"]) == track_id
    assert str(items[0]["title"]) == "Ad Item"
