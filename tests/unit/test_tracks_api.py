from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


class _FakeRuntimeRegistry:
    def __init__(self):
        self.stopped: list[int] = []

    def stop_station(self, station_id: int):
        self.stopped.append(int(station_id))
        return {"station_id": int(station_id), "running": False}


def test_tracks_create_and_query(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    create_res = client.post(
        "/api/tracks",
        json={
            "title": " Song One ",
            "artist": " Artist A ",
            "file_path": "C:/music/song-one.mp3",
            "musicbrainz_recordingid": "mbid-1",
        },
    )
    assert create_res.status_code == 200
    track_id = int(create_res.json()["track_id"])
    assert track_id > 0

    list_res = client.get("/api/tracks", params={"q": "Song One", "limit": 20})
    assert list_res.status_code == 200
    payload = list_res.json()
    assert payload["items"]
    assert any(int(item["id"]) == track_id for item in payload["items"])


def test_tracks_upsert_by_file_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    res1 = client.post(
        "/api/tracks",
        json={
            "title": "Track A",
            "artist": "Artist A",
            "file_path": "C:/music/dup.mp3",
        },
    )
    assert res1.status_code == 200
    id1 = int(res1.json()["track_id"])

    res2 = client.post(
        "/api/tracks",
        json={
            "title": "Track A (Updated)",
            "artist": "Artist A",
            "file_path": "C:/music/dup.mp3",
        },
    )
    assert res2.status_code == 200
    id2 = int(res2.json()["track_id"])
    assert id1 == id2

    get_res = client.get(f"/api/tracks/{id1}")
    assert get_res.status_code == 200
    assert get_res.json()["title"] == "Track A (Updated)"
    assert get_res.json()["exclude_from_autoplay"] is False
    assert int(get_res.json()["play_count"]) == 0
    assert get_res.json()["last_played_at"] == ""


def test_update_track_supports_autoplay_exclusion(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    created = client.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Original",
            "artist": "Artist",
            "album": "Album",
            "genre": "Pop",
            "language": "tr",
            "track_type": "music",
            "file_path": "C:/music/original.mp3",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    updated = client.put(
        f"/api/tracks/{track_id}",
        json={
            "title": "Updated",
            "artist": "Updated Artist",
            "album": "Updated Album",
            "genre": "Rock",
            "language": "en",
            "track_type": "jingle",
            "exclude_from_autoplay": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["ok"] is True

    fetched = client.get(f"/api/tracks/{track_id}")
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["title"] == "Updated"
    assert payload["artist"] == "Updated Artist"
    assert payload["album"] == "Updated Album"
    assert payload["genre"] == "Rock"
    assert payload["language"] == "en"
    assert payload["track_type"] == "jingle"
    assert payload["exclude_from_autoplay"] is True


def test_track_delete_hides_item_from_library_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    created = client.post(
        "/api/tracks",
        json={
            "title": "Delete Me",
            "artist": "Artist X",
            "file_path": "C:/music/delete-me.mp3",
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    removed = client.delete(f"/api/tracks/{track_id}")
    assert removed.status_code == 200
    payload = removed.json()
    assert payload.get("ok") is True
    assert payload.get("deleted") is True

    listed = client.get("/api/tracks", params={"q": "Delete Me", "limit": 20})
    assert listed.status_code == 200
    items = listed.json().get("items") or []
    assert all(int(item["id"]) != track_id for item in items)

    detail = client.get(f"/api/tracks/{track_id}")
    assert detail.status_code == 200
    assert detail.json().get("is_active") is False


def test_track_delete_returns_404_for_missing_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)
    res = client.delete("/api/tracks/999999")
    assert res.status_code == 404


def test_track_delete_cleans_queue_and_playlist_references(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    created = client.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Ref Track",
            "artist": "Ref Artist",
            "file_path": "C:/music/ref-track.mp3",
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, ?, 'pending')",
        (1, track_id, 1),
    )
    cur.execute(
        "INSERT INTO playlists (station_id, name, description, playlist_type) VALUES (?, ?, ?, ?)",
        (1, "P", "", "manual"),
    )
    playlist_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO playlist_items (playlist_id, track_id, position) VALUES (?, ?, 1)",
        (playlist_id, track_id),
    )
    cur.execute(
        "INSERT INTO program_queue_items (station_id, track_id, position) VALUES (?, ?, 1)",
        (1, track_id),
    )
    conn.commit()

    removed = client.delete(f"/api/tracks/{track_id}")
    assert removed.status_code == 200
    summary = removed.json()
    assert int(summary.get("queue_failed", 0)) >= 1
    assert int(summary.get("playlist_items_removed", 0)) >= 1
    assert int(summary.get("program_items_removed", 0)) >= 1

    cur.execute("SELECT status FROM queue_items WHERE track_id=?", (track_id,))
    row = cur.fetchone()
    assert row is not None
    assert str(row["status"]) == "failed"
    cur.execute("SELECT COUNT(*) AS c FROM playlist_items WHERE track_id=?", (track_id,))
    assert int(cur.fetchone()["c"]) == 0
    cur.execute("SELECT COUNT(*) AS c FROM program_queue_items WHERE track_id=?", (track_id,))
    assert int(cur.fetchone()["c"]) == 0


def test_track_delete_does_not_stop_runtime_when_only_pending_queue_item_is_removed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    created = client.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Pending Track",
            "artist": "Artist X",
            "file_path": "C:/music/pending-track.mp3",
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, ?, 'pending')",
        (1, track_id, 1),
    )
    conn.commit()

    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime)

    removed = client.delete(f"/api/tracks/{track_id}")
    assert removed.status_code == 200
    payload = removed.json()
    assert payload.get("ok") is True
    assert int(payload.get("queue_failed", 0)) == 1
    assert fake_runtime.stopped == []


def test_track_delete_stops_runtime_when_current_queue_item_is_playing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    created = client.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Playing Track",
            "artist": "Artist Y",
            "file_path": "C:/music/playing-track.mp3",
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, ?, 'playing', CURRENT_TIMESTAMP)",
        (1, track_id, 1),
    )
    conn.commit()

    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime)

    removed = client.delete(f"/api/tracks/{track_id}")
    assert removed.status_code == 200
    payload = removed.json()
    assert payload.get("ok") is True
    assert int(payload.get("queue_failed", 0)) == 1
    assert fake_runtime.stopped == [1]
