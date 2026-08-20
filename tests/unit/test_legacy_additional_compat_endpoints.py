from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def _create_track(client: TestClient, title: str, file_path: str, station_id: int = 1) -> int:
    res = client.post(
        "/api/tracks",
        json={
            "station_id": station_id,
            "title": title,
            "artist": "Compat Artist",
            "file_path": file_path,
            "track_type": "music",
        },
    )
    assert res.status_code == 200
    return int(res.json()["track_id"])


def test_legacy_additional_routes_exist_and_return_compatible_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    c = TestClient(app)

    speaker_get = c.get("/api/speaker/monitor")
    assert speaker_get.status_code == 200
    assert int((speaker_get.json() or {}).get("station_id") or 0) > 0

    speaker_put = c.put("/api/speaker/monitor", json={"station_id": 1})
    assert speaker_put.status_code == 200
    assert int((speaker_put.json() or {}).get("station_id") or 0) == 1

    refreshed = c.post("/api/queue/refresh", params={"station_id": 1})
    assert refreshed.status_code == 200
    assert "total" in refreshed.json()

    stats = c.get("/api/tracks/stats/summary", params={"station_id": 1})
    assert stats.status_code == 200
    stats_payload = stats.json()
    for key in ("total_tracks", "music_count", "ad_count", "library_scope"):
        assert key in stats_payload

    normalized = c.post(
        "/api/library/metadata/normalize",
        json={"station_id": 1, "analyze_bpm": True, "library_scope": "local"},
    )
    assert normalized.status_code == 200
    assert "summary" in normalized.json()

    verified = c.post(
        "/api/library/metadata/verify/itunes",
        json={"station_id": 1, "library_scope": "local", "track_type": "music"},
    )
    assert verified.status_code == 200
    verify_summary = (verified.json() or {}).get("summary") or {}
    assert "analyzed" in verify_summary

    imported = c.post(
        "/api/library/import/ytdlp",
        json={
            "url": "https://example.com/watch?v=legacy-sync-compat",
            "track_type": "music",
            "station_id": 1,
            "target_station_id": 1,
            "download_playlist": False,
            "music_only_mode": True,
            "audio_format": "mp3",
            "audio_quality": "192",
            "auto_trim_silence": False,
            "trim_threshold_db": -45,
            "trim_min_silence": 0.15,
            "auto_intro_clean": False,
            "intro_clean_preset": "normal",
            "intro_max_cut_s": 18,
        },
    )
    assert imported.status_code == 200
    imported_payload = imported.json()
    assert "scan" in imported_payload
    assert "downloaded_files" in imported_payload


def test_legacy_liquidsoap_next_and_played_routes_work_without_liquidsoap(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    c = TestClient(app)

    media = tmp_path / "compat-song.mp3"
    media.write_bytes(b"ID3")
    track_id = _create_track(c, "Compat Song", str(media), station_id=1)

    pushed = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": track_id})
    assert pushed.status_code == 200

    nxt = c.get("/api/liquidsoap/next", params={"station_id": 1})
    assert nxt.status_code == 200
    assert nxt.text.strip() == str(media)

    played = c.post(
        "/api/liquidsoap/played",
        json={
            "title": "Compat Song",
            "artist": "Compat Artist",
            "filename": str(media),
            "station_id": 1,
        },
    )
    assert played.status_code == 200
    assert played.json().get("ok") is True


def test_scanner_cleanup_soft_deactivates_missing_tracks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    c = TestClient(app)

    missing_path = tmp_path / "missing-song.mp3"
    track_id = _create_track(c, "Missing Song", str(missing_path), station_id=1)

    cleaned = c.post("/api/scanner/cleanup")
    assert cleaned.status_code == 200
    assert int((cleaned.json() or {}).get("removed") or 0) >= 1

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM tracks WHERE id=?", (track_id,))
    row = cur.fetchone()
    assert row is not None
    assert int(row["is_active"]) == 0
