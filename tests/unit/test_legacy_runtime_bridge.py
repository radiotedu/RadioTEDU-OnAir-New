from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.repositories.queue_repo import QueueRepository
from app.repositories.settings_repo import SettingsRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.running: dict[int, bool] = {}
        self.started: list[dict] = []
        self.stopped: list[int] = []

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float = 0.0,
    ):
        sid = int(station_id)
        self.started.append(
            {
                "station_id": sid,
                "input_uri": str(input_uri),
                "stream_title": str(stream_title or ""),
                "stream_artist": str(stream_artist or ""),
                "track_type": str(track_type or "music"),
                "crossfade_seconds": float(crossfade_seconds),
            }
        )
        self.running[sid] = True
        return self.status(sid)

    def stop_station(self, station_id: int):
        sid = int(station_id)
        self.stopped.append(sid)
        self.running[sid] = False
        return self.status(sid)

    def status(self, station_id: int):
        sid = int(station_id)
        running = bool(self.running.get(sid, False))
        return {
            "station_id": sid,
            "running": running,
            "branch_health": {"icecast": running, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }


def test_station_settings_syncs_station_output_and_push_starts_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    SettingsRepository(get_connection()).upsert_system({"default_crossfade_seconds": "3.0"})
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)

    audio_file = tmp_path / "runtime-song.mp3"
    audio_file.write_bytes(b"ID3")

    c = TestClient(app)
    created = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Runtime Song",
            "artist": "Tester",
            "file_path": str(audio_file),
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    save_settings = c.put(
        "/api/settings/station",
        json={
            "station_id": 1,
            "output_mode": "icecast",
            "speaker_monitor_enabled": False,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/live",
            "icecast_username": "source",
            "icecast_password": "hackme",
            "output_gain_db": -1.5,
        },
    )
    assert save_settings.status_code == 200

    output_cfg = c.get("/api/stations/output", params={"station_id": 1})
    assert output_cfg.status_code == 200
    out = output_cfg.json()
    assert out["icecast_enabled"] is True
    assert out["icecast_mount"] == "/live"
    assert out["local_output_enabled"] is False

    pushed = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": track_id})
    assert pushed.status_code == 200
    pushed_data = pushed.json()
    assert pushed_data.get("runtime_started") is True
    assert fake_runtime.started
    assert fake_runtime.started[-1]["station_id"] == 1
    assert fake_runtime.started[-1]["input_uri"].endswith("runtime-song.mp3")
    assert fake_runtime.started[-1]["stream_title"] == "Runtime Song"
    assert fake_runtime.started[-1]["stream_artist"] == "Tester"
    assert fake_runtime.started[-1]["track_type"] == "music"
    assert float(fake_runtime.started[-1]["crossfade_seconds"]) == 3.0

    q = c.get("/api/queue", params={"station_id": 1})
    assert q.status_code == 200
    items = q.json().get("items") or []
    assert items
    assert items[0]["status"] == "playing"

    skipped = c.post("/api/liquidsoap/skip", params={"station_id": 1})
    assert skipped.status_code == 200
    data = skipped.json()
    assert data.get("skipped") is True
    assert data.get("started_next") is False
    assert 1 in fake_runtime.stopped


def test_push_reports_runtime_error_when_track_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    c = TestClient(app)

    created = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Missing File Track",
            "artist": "Tester",
            "file_path": str(tmp_path / "missing.mp3"),
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    pushed = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": track_id})
    assert pushed.status_code == 200
    payload = pushed.json()
    assert payload.get("runtime_started") is False
    assert "input file not found" in str(payload.get("runtime_error") or "")


def test_push_remaps_stale_library_path_to_current_uploads_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)

    audio_file = tmp_path / "uploads" / "station-1" / "music" / "legacy-song.mp3"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"ID3")
    stale_file_path = (
        "E:/liquidsoap-2.4.2-win64/radio-automation/cleanroom/backend/"
        "data/uploads/station-1/music/legacy-song.mp3"
    )

    c = TestClient(app)
    created = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Legacy Song",
            "artist": "Tester",
            "file_path": stale_file_path,
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    pushed = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": track_id})
    assert pushed.status_code == 200
    payload = pushed.json()
    assert payload.get("runtime_started") is True
    assert fake_runtime.started[-1]["input_uri"] == str(audio_file.resolve())


def test_push_on_active_music_forwards_transition_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    SettingsRepository(get_connection()).upsert_system({"default_crossfade_seconds": "3.0"})
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)

    first_audio = tmp_path / "first-song.mp3"
    second_audio = tmp_path / "second-song.mp3"
    first_audio.write_bytes(b"ID3")
    second_audio.write_bytes(b"ID3")

    c = TestClient(app)
    created_first = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "First Song",
            "artist": "Artist One",
            "file_path": str(first_audio),
            "track_type": "music",
        },
    )
    created_second = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Second Song",
            "artist": "Artist Two",
            "file_path": str(second_audio),
            "track_type": "music",
        },
    )
    assert created_first.status_code == 200
    assert created_second.status_code == 200

    first_track_id = int(created_first.json()["track_id"])
    second_track_id = int(created_second.json()["track_id"])

    first_push = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": first_track_id})
    second_push = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": second_track_id})

    assert first_push.status_code == 200
    assert second_push.status_code == 200
    assert len(fake_runtime.started) == 2
    assert fake_runtime.started[-1]["stream_title"] == "Second Song"
    assert fake_runtime.started[-1]["stream_artist"] == "Artist Two"
    assert fake_runtime.started[-1]["track_type"] == "music"
    assert float(fake_runtime.started[-1]["crossfade_seconds"]) == 3.0


def test_skip_starts_next_music_track_with_transition_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)

    current_audio = tmp_path / "current-song.mp3"
    next_audio = tmp_path / "next-song.mp3"
    current_audio.write_bytes(b"ID3")
    next_audio.write_bytes(b"ID3")

    c = TestClient(app)
    created_current = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Current Song",
            "artist": "Artist One",
            "file_path": str(current_audio),
            "track_type": "music",
        },
    )
    created_next = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Next Song",
            "artist": "Artist Two",
            "file_path": str(next_audio),
            "track_type": "music",
        },
    )
    assert created_current.status_code == 200
    assert created_next.status_code == 200

    current_track_id = int(created_current.json()["track_id"])
    next_track_id = int(created_next.json()["track_id"])

    first_push = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": current_track_id})
    assert first_push.status_code == 200

    QueueRepository(conn).enqueue(
        station_id=1,
        track_id=next_track_id,
        dedupe_key=f"legacy-next:{next_track_id}",
    )

    skipped = c.post("/api/liquidsoap/skip", params={"station_id": 1})
    assert skipped.status_code == 200
    payload = skipped.json()
    assert payload.get("started_next") is True
    assert fake_runtime.started[-1]["stream_title"] == "Next Song"
    assert fake_runtime.started[-1]["stream_artist"] == "Artist Two"
    assert fake_runtime.started[-1]["track_type"] == "music"
    assert float(fake_runtime.started[-1]["crossfade_seconds"]) == 3.0


def test_skip_autofills_next_music_track_when_queue_only_has_current_item(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)

    current_audio = tmp_path / "current-song.mp3"
    library_audio = tmp_path / "library-song.mp3"
    current_audio.write_bytes(b"ID3")
    library_audio.write_bytes(b"ID3")

    c = TestClient(app)
    created_current = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Current Song",
            "artist": "Artist One",
            "file_path": str(current_audio),
            "track_type": "music",
            "play_count": 10,
        },
    )
    created_library = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Library Song",
            "artist": "Artist Two",
            "file_path": str(library_audio),
            "track_type": "music",
        },
    )
    assert created_current.status_code == 200
    assert created_library.status_code == 200

    current_track_id = int(created_current.json()["track_id"])
    library_track_id = int(created_library.json()["track_id"])
    conn.cursor().execute(
        "UPDATE tracks SET play_count=10 WHERE id=?",
        (current_track_id,),
    )
    conn.commit()

    first_push = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": current_track_id})
    assert first_push.status_code == 200

    skipped = c.post("/api/liquidsoap/skip", params={"station_id": 1})
    assert skipped.status_code == 200
    payload = skipped.json()
    assert payload.get("started_next") is True
    assert int(payload.get("next_item_id") or 0) > 0
    assert fake_runtime.started[-1]["stream_title"] == "Library Song"
    assert fake_runtime.started[-1]["stream_artist"] == "Artist Two"
    assert fake_runtime.started[-1]["track_type"] == "music"
    assert float(fake_runtime.started[-1]["crossfade_seconds"]) == 3.0

    rows = conn.cursor().execute(
        "SELECT track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows][-1] == (
        library_track_id,
        "playing",
    )


def test_skip_starts_sweeper_before_next_music_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "1",
            "sweeper_mode": "random",
        },
    )
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)

    current_audio = tmp_path / "current-song.mp3"
    next_audio = tmp_path / "next-song.mp3"
    sweeper_audio = tmp_path / "station-sweeper.mp3"
    current_audio.write_bytes(b"ID3")
    next_audio.write_bytes(b"ID3")
    sweeper_audio.write_bytes(b"ID3")

    c = TestClient(app)
    created_current = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Current Song",
            "artist": "Artist One",
            "file_path": str(current_audio),
            "track_type": "music",
            "play_count": 10,
        },
    )
    created_next = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Next Song",
            "artist": "Artist Two",
            "file_path": str(next_audio),
            "track_type": "music",
        },
    )
    created_sweeper = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Station Sweeper",
            "artist": "Voice",
            "file_path": str(sweeper_audio),
            "track_type": "jingle",
        },
    )
    assert created_current.status_code == 200
    assert created_next.status_code == 200
    assert created_sweeper.status_code == 200

    current_track_id = int(created_current.json()["track_id"])
    next_track_id = int(created_next.json()["track_id"])
    sweeper_track_id = int(created_sweeper.json()["track_id"])
    conn.cursor().execute(
        "UPDATE tracks SET play_count=10 WHERE id=?",
        (current_track_id,),
    )
    conn.commit()

    first_push = c.post("/api/liquidsoap/push", params={"station_id": 1, "track_id": current_track_id})
    assert first_push.status_code == 200

    skipped = c.post("/api/liquidsoap/skip", params={"station_id": 1})
    assert skipped.status_code == 200
    payload = skipped.json()
    assert payload.get("started_next") is True
    assert fake_runtime.started[-1]["stream_title"] == "Station Sweeper"
    assert fake_runtime.started[-1]["track_type"] == "jingle"

    rows = conn.cursor().execute(
        "SELECT q.track_id, q.status, COALESCE(t.track_type, 'music') AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [
        (int(row["track_id"]), str(row["status"]), str(row["track_type"]))
        for row in rows[-3:]
    ] == [
        (current_track_id, "done", "music"),
        (sweeper_track_id, "playing", "jingle"),
        (next_track_id, "pending", "music"),
    ]


def test_sweeper_config_toggle_reconciles_pending_queue_items(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "1",
            "sweeper_mode": "ordered",
        },
    )
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Current Song", "Artist", "music", "C:/music/current.mp3"),
    )
    current_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Next Song", "Artist", "music", "C:/music/next.mp3"),
    )
    next_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Station Sweeper", "Voice", "jingle", "C:/jingles/sweeper.mp3"),
    )
    sweeper_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', CURRENT_TIMESTAMP)",
        (1, current_track_id),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) VALUES (?, ?, 2, 'pending', ?)",
        (1, sweeper_track_id, f"jingle:{sweeper_track_id}:2"),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, 3, 'pending')",
        (1, next_track_id),
    )
    conn.commit()
    conn.close()

    c = TestClient(app)

    disabled = c.post(
        "/api/sweeper/config",
        json={"station_id": 1, "enabled": False, "interval": 1, "mode": "ordered"},
    )
    assert disabled.status_code == 200
    disabled_rows = get_connection().cursor().execute(
        "SELECT q.track_id, q.status, COALESCE(t.track_type, 'music') AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 AND q.status IN ('playing', 'pending') "
        "ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [int(row["track_id"]) for row in disabled_rows[:2]] == [
        current_track_id,
        next_track_id,
    ]
    assert all(str(row["track_type"]) != "jingle" for row in disabled_rows)

    enabled = c.post(
        "/api/sweeper/config",
        json={"station_id": 1, "enabled": True, "interval": 1, "mode": "ordered"},
    )
    assert enabled.status_code == 200
    enabled_rows = get_connection().cursor().execute(
        "SELECT q.track_id, q.status, COALESCE(t.track_type, 'music') AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 AND q.status IN ('playing', 'pending') "
        "ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [int(row["track_id"]) for row in enabled_rows[:3]] == [
        current_track_id,
        sweeper_track_id,
        next_track_id,
    ]


def test_sweeper_config_enable_populates_next_queue_when_only_current_track_exists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "false",
            "sweeper_interval": "1",
            "sweeper_mode": "ordered",
        },
    )
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count) VALUES (?, ?, ?, ?, ?, 1, 10)",
        (1, "Current Song", "Artist", "music", "C:/music/current.mp3"),
    )
    current_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
        (1, "Library Song", "Artist", "music", "C:/music/library.mp3"),
    )
    library_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Station Sweeper", "Voice", "jingle", "C:/jingles/sweeper.mp3"),
    )
    sweeper_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', CURRENT_TIMESTAMP)",
        (1, current_track_id),
    )
    conn.commit()
    conn.close()

    c = TestClient(app)
    enabled = c.post(
        "/api/sweeper/config",
        json={"station_id": 1, "enabled": True, "interval": 1, "mode": "ordered"},
    )
    assert enabled.status_code == 200

    rows = get_connection().cursor().execute(
        "SELECT q.track_id, q.status, COALESCE(t.track_type, 'music') AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 AND q.status IN ('playing', 'pending') "
        "ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"]), str(row["track_type"])) for row in rows[:3]] == [
        (current_track_id, "playing", "music"),
        (sweeper_track_id, "pending", "jingle"),
        (library_track_id, "pending", "music"),
    ]


def test_sweeper_config_enable_without_jingles_stays_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "false",
            "sweeper_interval": "3",
            "sweeper_mode": "random",
        },
    )
    conn.close()

    c = TestClient(app)
    res = c.post(
        "/api/sweeper/config",
        json={"station_id": 1, "enabled": True, "interval": 2, "mode": "random"},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["enabled"] is False
    assert payload["jingle_count"] == 0
    assert payload["reason"] == "no_jingles"

    saved = SettingsRepository(get_connection()).get_station(1)
    assert str(saved.get("sweeper_enabled")) == "false"
    assert str(saved.get("sweeper_interval")) == "2"
    assert str(saved.get("sweeper_mode")) == "random"

    config = c.get("/api/sweeper/config", params={"station_id": 1})
    assert config.status_code == 200
    config_payload = config.json()
    assert config_payload["enabled"] is False
    assert config_payload["jingle_count"] == 0
