import asyncio
import io
import json

from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.api.legacy import legacy_upload_import
from app.db import get_connection, init_db
from app.main import app


def test_liquidsoap_status_and_cart_routes_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    status_res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert "alive" in status_data
    assert "active_station_id" in status_data
    assert "program_queue_source" in status_data

    cart_res = c.post(
        "/api/liquidsoap/cart",
        params={"station_id": 1, "file_path": "C:/media/jingle.mp3"},
    )
    assert cart_res.status_code == 200
    assert cart_res.json().get("ok") is True


def test_ytdlp_compat_endpoints_exist_and_return_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    c = TestClient(app)

    settings = c.get("/api/library/import/ytdlp/settings", params={"station_id": 1})
    assert settings.status_code == 200
    settings_data = settings.json()
    assert "binary" in settings_data
    assert isinstance(settings_data.get("stations"), list)

    snapshot_before = c.get(
        "/api/library/import/ytdlp/jobs/status", params={"limit_recent": 25}
    )
    assert snapshot_before.status_code == 200
    before_data = snapshot_before.json()
    assert isinstance(before_data.get("queue"), list)
    assert isinstance(before_data.get("recent"), list)
    assert isinstance(before_data.get("counts"), dict)

    queued = c.post(
        "/api/library/import/ytdlp/jobs",
        json={
            "url": "https://example.com/watch?v=abc123",
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
    assert queued.status_code == 200
    job = (queued.json() or {}).get("job") or {}
    job_id = str(job.get("id") or "")
    assert job_id

    snapshot_after = c.get(
        "/api/library/import/ytdlp/jobs/status", params={"limit_recent": 25}
    )
    assert snapshot_after.status_code == 200
    after_data = snapshot_after.json()
    visible_jobs = [
        item
        for item in [after_data.get("running")] + list(after_data.get("queue", [])) + list(after_data.get("recent", []))
        if item
    ]
    assert any(str(item.get("id")) == job_id for item in visible_jobs)

    detail = c.get(f"/api/library/import/ytdlp/jobs/{job_id}")
    assert detail.status_code == 200
    assert str(detail.json().get("id")) == job_id


def test_ytdlp_settings_uses_shared_binary_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    def _fake_resolve_binary(name: str) -> str | None:
        mapping = {
            "yt-dlp.exe": "C:/tools/bin/yt-dlp.exe",
            "yt-dlp": "C:/tools/bin/yt-dlp.exe",
            "ffmpeg.exe": "C:/tools/bin/ffmpeg.exe",
            "ffmpeg": "C:/tools/bin/ffmpeg.exe",
        }
        return mapping.get(name)

    monkeypatch.setattr("app.api.legacy.resolve_binary", _fake_resolve_binary)

    c = TestClient(app)
    settings = c.get("/api/library/import/ytdlp/settings", params={"station_id": 1})
    assert settings.status_code == 200
    payload = settings.json()
    assert payload["binary_found"] is True
    assert payload["binary_path"] == "C:/tools/bin/yt-dlp.exe"
    assert payload["ffmpeg_found"] is True
    assert payload["ffmpeg_path"] == "C:/tools/bin/ffmpeg.exe"


def test_favicon_route_exists():
    c = TestClient(app)
    res = c.get("/favicon.ico")
    assert res.status_code == 200


def test_api_queue_autofills_empty_broadcast_queue_with_least_played_tracks(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    rows = [
        ("Song C", "C:/music/c.mp3", 3, "2026-03-13 10:00:00", 0),
        ("Song A", "C:/music/a.mp3", 0, None, 0),
        ("Song B", "C:/music/b.mp3", 0, "2026-03-12 10:00:00", 0),
        ("Blocked", "C:/music/d.mp3", 0, None, 1),
    ]
    for title, file_path, play_count, last_played_at, exclude_from_autoplay in rows:
        cur.execute(
            "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, "
            "last_played_at, exclude_from_autoplay) VALUES (1, ?, '', 'music', ?, 1, ?, ?, ?)",
            (title, file_path, play_count, last_played_at, exclude_from_autoplay),
        )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/queue", params={"station_id": 1})
    assert res.status_code == 200
    payload = res.json()
    titles = [str(item["title"]) for item in payload["items"]]
    assert titles[:3] == ["Song A", "Song B", "Song C"]
    assert "Blocked" not in titles
    assert int(payload["total"]) >= 3


def test_api_queue_does_not_autofill_when_broadcast_queue_already_has_items(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Queued Song', '', 'music', 'C:/music/queued.mp3', 1, 8, 0)"
    )
    queued_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Candidate Song', '', 'music', 'C:/music/candidate.mp3', 1, 0, 0)"
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, 1, 'pending')",
        (1, queued_track_id),
    )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/queue", params={"station_id": 1})
    assert res.status_code == 200
    payload = res.json()
    assert payload["total"] == 1
    assert [str(item["title"]) for item in payload["items"]] == ["Queued Song"]


def test_upload_import_returns_imported_track_ids_and_autoplay_defaults(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    audio_path = tmp_path / "upload-song.mp3"
    audio_path.write_bytes(b"ID3")

    c = TestClient(app)
    with audio_path.open("rb") as handle:
        res = c.post(
            "/api/library/import/upload",
            data={
                "station_id": "1",
                "target_station_id": "1",
                "track_type": "music",
                "auto_trim_silence": "false",
                "auto_intro_clean": "false",
            },
            files={"files": ("upload-song.mp3", handle, "audio/mpeg")},
        )
    assert res.status_code == 200
    payload = res.json()
    imported_track_ids = payload.get("imported_track_ids") or []
    assert len(imported_track_ids) == 1

    track_id = int(imported_track_ids[0])
    detail = c.get(f"/api/tracks/{track_id}")
    assert detail.status_code == 200
    track = detail.json()
    assert track["title"] == "upload-song"
    assert track["exclude_from_autoplay"] is False


def test_upload_import_extracts_artist_and_duration_from_ffprobe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    audio_path = tmp_path / "asik-oluyorum-eyvah.mp3"
    payload = b"ID3"
    audio_path.write_bytes(payload)

    def _fake_resolve_binary(name: str) -> str | None:
        if name in {"ffprobe", "ffprobe.exe"}:
            return "ffprobe"
        return None

    def _fake_run(cmd, capture_output, text, encoding, errors, timeout):
        assert cmd[0] == "ffprobe"
        assert str(audio_path.name) in str(cmd[-1])
        assert encoding == "utf-8"
        assert errors == "replace"

        class _Result:
            returncode = 0
            stdout = json.dumps(
                {
                    "format": {
                        "duration": "215.5",
                        "tags": {
                            "title": "Asik Oluyorum Eyvah",
                            "artist": "MFO",
                        },
                    }
                }
            )

        return _Result()

    monkeypatch.setattr("app.api.legacy.resolve_binary", _fake_resolve_binary)
    monkeypatch.setattr("app.api.legacy.subprocess.run", _fake_run)
    monkeypatch.setattr("app.auth.password.hash_password", lambda _: "stub-hash")

    upload = UploadFile(filename=audio_path.name, file=io.BytesIO(payload))

    result = asyncio.run(
        legacy_upload_import(
            station_id=1,
            target_station_id=1,
            track_type="music",
            auto_trim_silence=False,
            auto_intro_clean=False,
            files=[upload],
        )
    )
    track_id = int(result["imported_track_ids"][0])

    conn = get_connection()
    row = conn.execute(
        "SELECT title, artist, duration FROM tracks WHERE id=?",
        (track_id,),
    ).fetchone()
    assert row is not None
    assert row["title"] == "Asik Oluyorum Eyvah"
    assert row["artist"] == "MFO"
    assert row["duration"] == 215.5
