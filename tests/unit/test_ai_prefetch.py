from app.db import get_connection, init_db
from app.services.ai_prefetch import AIPrefetchService, PrefetchTarget


def test_generate_track_intro_passes_dedupe_key_to_ai_service():
    captured: dict[str, object] = {}

    class _FakeAI:
        def generate_track_intro_announcement(self, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

    service = AIPrefetchService()
    target = PrefetchTarget(
        item_id=7,
        title="Blue in Green",
        artist="Miles Davis",
        announcement_type="track_intro",
        dedupe_key="ai-track-intro:7",
        station_id=1,
        station_name="Radio TEDU",
        settings={},
    )

    result = service._generate_announcement(_FakeAI(), target, {})

    assert result == {"ok": True}
    assert captured["dedupe_key"] == "ai-track-intro:7"


def test_identify_targets_skips_non_music_items():
    service = AIPrefetchService()

    targets = service._identify_targets(
        1,
        [
            {"id": 10, "title": "Already an intro", "artist": "AI Host", "track_type": "announcement"},
            {"id": 11, "title": "Kind of Blue", "artist": "Miles Davis", "track_type": "music"},
        ],
        "Radio TEDU",
        {"ai_station_id_interval": "0"},
    )

    assert [(target.item_id, target.announcement_type) for target in targets] == [
        (11, "track_intro"),
    ]


def test_identify_targets_prefers_track_intros_and_only_one_station_id():
    service = AIPrefetchService()

    targets = service._identify_targets(
        1,
        [
            {"id": 11, "title": "Kind of Blue", "artist": "Miles Davis", "track_type": "music"},
            {"id": 12, "title": "Blue Train", "artist": "John Coltrane", "track_type": "music"},
        ],
        "Radio TEDU",
        {"ai_station_id_interval": "1800"},
    )

    assert [(target.item_id, target.announcement_type) for target in targets] == [
        (11, "track_intro"),
        (12, "track_intro"),
        (11, "station_id"),
    ]


def test_get_upcoming_items_returns_pending_rows_from_active_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Current Song', 'Artist A', 'music', 30, 'C:/music/current.mp3', 1, 0, 0)"
    )
    current_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Next Song', 'Artist B', 'music', 30, 'C:/music/next.mp3', 1, 0, 0)"
    )
    next_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Station ID', 'AI Host', 'announcement', 5, 'C:/audio/station-id.wav', 1, 0, 0)"
    )
    announcement_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'playing')",
        (current_track_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
        (next_track_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 3, 'pending')",
        (announcement_track_id,),
    )
    conn.commit()

    items = AIPrefetchService._get_upcoming_items(1, 5)

    assert [str(item["status"]) for item in items] == ["pending", "pending"]
    assert [int(item["track_id"]) for item in items] == [next_track_id, announcement_track_id]


def test_get_upcoming_items_counts_music_lookahead_not_raw_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Intro One', 'AI Host', 'announcement', 5, 'C:/audio/intro1.wav', 1, 0, 0)"
    )
    intro_one_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song One', 'Artist A', 'music', 30, 'C:/music/one.mp3', 1, 0, 0)"
    )
    song_one_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Intro Two', 'AI Host', 'announcement', 5, 'C:/audio/intro2.wav', 1, 0, 0)"
    )
    intro_two_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song Two', 'Artist B', 'music', 30, 'C:/music/two.mp3', 1, 0, 0)"
    )
    song_two_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'pending')",
        (intro_one_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
        (song_one_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 3, 'pending')",
        (intro_two_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 4, 'pending')",
        (song_two_id,),
    )
    conn.commit()

    items = AIPrefetchService._get_upcoming_items(1, 2)

    assert [int(item["track_id"]) for item in items] == [
        intro_one_id,
        song_one_id,
        intro_two_id,
        song_two_id,
    ]


def test_prime_station_generates_first_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song One', 'Artist A', 'music', 30, 'C:/music/one.mp3', 1, 0, 0)"
    )
    first_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song Two', 'Artist B', 'music', 30, 'C:/music/two.mp3', 1, 0, 0)"
    )
    second_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'pending')",
        (first_track_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
        (second_track_id,),
    )
    conn.commit()

    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_settings",
        staticmethod(lambda station_id: {"ai_host_enabled": "true", "ai_station_id_interval": "0"}),
    )
    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_name",
        staticmethod(lambda station_id: "Radio TEDU"),
    )
    monkeypatch.setattr(
        AIPrefetchService,
        "_is_cached",
        lambda self, dedupe_key, expected_tts_provider=None: False,
    )

    calls: list[str] = []

    def _fake_generate(self, ai, target, settings):
        calls.append(target.dedupe_key)
        return {"ok": True}

    monkeypatch.setattr(AIPrefetchService, "_generate_announcement", _fake_generate)
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: object())

    service = AIPrefetchService()
    result = service.prime_station(1, max_generate=2)

    assert result["generated"] == 2
    assert calls == ["ai-track-intro:1", "ai-track-intro:2"]


def test_prefetch_helpers_read_station_settings_and_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO stations (name) VALUES ('Radio TEDU')")
    station_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value) VALUES (?, 'ai_host_enabled', 'true')",
        (station_id,),
    )
    conn.commit()
    conn.close()

    settings = AIPrefetchService._get_station_settings(station_id)
    name = AIPrefetchService._get_station_name(station_id)

    assert settings["ai_host_enabled"] == "true"
    assert name == "Radio TEDU"


def test_prime_station_skips_slow_omnivoice_provider(monkeypatch):
    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_settings",
        staticmethod(lambda station_id: {"ai_host_enabled": "true", "ai_tts_provider": "omnivoice"}),
    )

    service = AIPrefetchService()
    result = service.prime_station(1, max_generate=2)

    assert result["reason"] == "slow_tts_provider"
    assert result["generated"] == 0


def test_startup_prime_station_allows_first_omnivoice_intro(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song One', 'Artist A', 'music', 30, 'C:/music/one.mp3', 1, 0, 0)"
    )
    first_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'pending')",
        (first_track_id,),
    )
    conn.commit()

    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_settings",
        staticmethod(lambda station_id: {"ai_host_enabled": "true", "ai_tts_provider": "omnivoice"}),
    )
    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_name",
        staticmethod(lambda station_id: "Radio TEDU"),
    )
    monkeypatch.setattr(
        AIPrefetchService,
        "_is_cached",
        lambda self, dedupe_key, expected_tts_provider=None: False,
    )
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: object())

    calls: list[str] = []

    def _fake_generate(self, ai, target, settings):
        calls.append(target.dedupe_key)
        return {"ok": True}

    monkeypatch.setattr(AIPrefetchService, "_generate_announcement", _fake_generate)

    service = AIPrefetchService()
    result = service.startup_prime_station(1)

    assert result["tts_provider"] == "omnivoice"
    assert result["allow_slow_tts"] is True
    assert result["generated"] == 1
    assert result["lookahead"] == 2
    assert calls == ["ai-track-intro:1"]


def test_batch_generate_station_allows_omnivoice_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song One', 'Artist A', 'music', 30, 'C:/music/one.mp3', 1, 0, 0)"
    )
    first_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'pending')",
        (first_track_id,),
    )
    conn.commit()

    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_settings",
        staticmethod(lambda station_id: {"ai_host_enabled": "true", "ai_tts_provider": "omnivoice"}),
    )
    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_name",
        staticmethod(lambda station_id: "Radio TEDU"),
    )
    monkeypatch.setattr(
        AIPrefetchService,
        "_is_cached",
        lambda self, dedupe_key, expected_tts_provider=None: False,
    )
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: object())

    calls: list[str] = []

    def _fake_generate(self, ai, target, settings):
        calls.append(target.dedupe_key)
        return {"ok": True}

    monkeypatch.setattr(AIPrefetchService, "_generate_announcement", _fake_generate)

    service = AIPrefetchService()
    result = service.batch_generate_station(1, max_generate=1, lookahead=2)

    assert result["tts_provider"] == "omnivoice"
    assert result["generated"] == 1
    assert result["targets"] == 1
    assert calls == ["ai-track-intro:1"]


def test_is_cached_requires_matching_tts_provider(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"R" * 32)
    (tmp_path / "announcement_test.json").write_text(
        '{"dedupe_key":"ai-track-intro:77","audio_path":"'
        + str(audio_path).replace("\\", "\\\\")
        + '","tts_provider":"edge-tts"}',
        encoding="utf-8",
    )

    service = AIPrefetchService()

    assert service._is_cached("ai-track-intro:77", expected_tts_provider="edge-tts") is True
    assert service._is_cached("ai-track-intro:77", expected_tts_provider="omnivoice") is False


def test_readiness_snapshot_counts_matching_prefetched_intros(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song One', 'Artist A', 'music', 120, 'C:/music/one.mp3', 1, 0, 0)"
    )
    first_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Song Two', 'Artist B', 'music', 180, 'C:/music/two.mp3', 1, 0, 0)"
    )
    second_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'pending')",
        (first_track_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
        (second_track_id,),
    )
    conn.commit()

    monkeypatch.setattr(
        AIPrefetchService,
        "_get_station_settings",
        staticmethod(lambda station_id: {"ai_host_enabled": "true", "ai_tts_provider": "omnivoice"}),
    )

    audio_path = tmp_path / "intro.wav"
    audio_path.write_bytes(b"O" * 64)
    (tmp_path / "announcement_ready.json").write_text(
        '{"dedupe_key":"ai-track-intro:1","audio_path":"'
        + str(audio_path).replace("\\", "\\\\")
        + '","tts_provider":"omnivoice"}',
        encoding="utf-8",
    )

    service = AIPrefetchService()
    snapshot = service.readiness_snapshot(1, lookahead=2)

    assert snapshot["tts_provider"] == "omnivoice"
    assert snapshot["upcoming_music_items"] == 2
    assert snapshot["ready_track_intros"] == 1
    assert snapshot["covered_music_seconds"] == 120.0
    assert snapshot["next_missing_track_intro_item_ids"] == [2]
