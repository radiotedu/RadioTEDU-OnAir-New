import threading
import time
import builtins
import json

import app.engine.runtime_registry as runtime_registry_module
from app.audio.gst_pipeline import StationPipelineConfig
from app.db import get_connection, init_db
from app.engine.runtime_registry import StationRuntimeRegistry
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository


def test_station_names_select_expected_genre_processing_profiles():
    expected = {
        "RadioTEDU Classical": "classical",
        "RadioTEDU Lo-Fi": "lofi",
        "RadioTEDU Pop": "pop",
        "RadioTEDU Jazz": "jazz",
        "RadioTEDU Rock": "rock",
        "RadioTEDU Energize": "energize",
    }
    assert {
        name: runtime_registry_module._default_processing_profile_for_station(name)
        for name in expected
    } == expected


def test_stale_high_quality_profile_self_heals_to_aac_low_192():
    settings = {
        "station_1_extra_icecast_outputs": json.dumps(
            [
                {
                    "enabled": True,
                    "quality": "high",
                    "icecast_mount": "/classic-high",
                    "stream_codec_profile": "opus_128",
                    "stream_bitrate_kbps": 128,
                }
            ]
        )
    }
    row = {
        "icecast_host": "stream.example.test",
        "icecast_port": 8000,
        "icecast_user": "source",
        "icecast_password": "secret",
        "stream_codec_profile": "opus_96",
        "stream_bitrate_kbps": 96,
    }

    outputs = runtime_registry_module._extra_icecast_outputs(settings, 1, row)

    assert outputs[0]["stream_codec_profile"] == "aac_low_192"
    assert outputs[0]["stream_bitrate_kbps"] == 192


def test_lofi_forces_metadata_suppression_on_all_quality_outputs():
    settings = {
        "station_2_extra_icecast_outputs": json.dumps(
            [
                {
                    "enabled": True,
                    "quality": "low",
                    "icecast_mount": "/lofi-low",
                    # A legacy explicit false must not re-enable Lo-Fi metadata.
                    "metadata_suppressed": False,
                }
            ]
        )
    }
    row = {
        "icecast_host": "stream.example.test",
        "icecast_port": 8000,
        "icecast_user": "source",
        "icecast_password": "secret",
        "stream_codec_profile": "he_aac_192",
        "stream_bitrate_kbps": 192,
    }

    outputs = runtime_registry_module._extra_icecast_outputs(settings, 2, row)

    assert outputs[0]["metadata_suppressed"] is True
    assert outputs[0]["stream_codec_profile"] == "aac_he_v2_64"
    assert outputs[0]["stream_bitrate_kbps"] == 64


def test_ai_runtime_status_uses_persisted_readiness_without_cache_scan(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "app.services.ai_prefetch":
            raise AssertionError("runtime status must not scan AI cache")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    snapshot = runtime_registry_module._ai_startup_status_snapshot(
        9,
        {
            "ai_host_enabled": "true",
            "startup_ai_readiness_state": "warming",
            "startup_ai_ready_intro_count": "2",
            "startup_ai_required_intro_count": "4",
        },
    )

    assert snapshot == {
        "ai_startup_state": "warming",
        "ai_ready_intro_count": 2,
        "ai_required_intro_count": 4,
    }


class _FakeRuntime:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.last_cfg = None
        self.refreshed_outputs = None

    def start(self, cfg, **_kwargs):
        self.started = True
        self.last_cfg = cfg

    def stop(self):
        self.stopped = True
        self.started = False

    def is_running(self):
        return self.started

    def branch_health(self):
        return {"icecast": True, "local": True}

    def refresh_extra_icecast_outputs(self, outputs):
        self.refreshed_outputs = tuple(outputs)
        return {"running": self.started, "producer_preserved": True}


class _RecoveringRuntime(_FakeRuntime):
    def __init__(self, *, fail_recovery=False):
        super().__init__()
        self.healthy = False
        self.fail_recovery = fail_recovery
        self.recover_calls = 0

    def branch_health(self):
        return {"icecast": self.healthy, "local": True}

    def recover_outputs(self):
        self.recover_calls += 1
        if self.fail_recovery:
            raise RuntimeError("connection refused")
        self.healthy = True
        return {"running": True, "branch_health": self.branch_health()}


class _FlowingUnverifiedRuntime(_RecoveringRuntime):
    def status(self):
        return {
            "running": False,
            "program_running": True,
            "program_pcm_age_seconds": 0.02,
            "program_pcm_stalled": False,
            "output_feed_active": False,
            "branch_health": self.branch_health(),
            "icecast_mount_health": {
                "process_running": True,
                "mount_healthy": False,
                "writer_running": True,
                "writer_failed": False,
                "writer_backpressured": False,
                "last_write_age_seconds": 0.01,
            },
        }


def test_registry_serializes_operations_for_the_same_station():
    reg = StationRuntimeRegistry(runtime_factory=lambda: _FakeRuntime())
    entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_start(**_kwargs):
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
            entered.set()
        release.wait(2.0)
        with state_lock:
            active -= 1
        return {"running": True}

    reg._start_station_unlocked = fake_start
    threads = [
        threading.Thread(target=reg.start_station, args=(1, "C:/one.mp3")),
        threading.Thread(target=reg.start_station, args=(1, "C:/two.mp3")),
    ]
    threads[0].start()
    assert entered.wait(0.5)
    threads[1].start()
    time.sleep(0.05)
    assert maximum == 1
    release.set()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not any(thread.is_alive() for thread in threads)
    assert maximum == 1


def test_registry_start_status_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=True,
        output_device_id="dev1",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="hackme",
        output_gain_db=0.0,
        stream_codec_profile="mp3_128",
        stream_bitrate_kbps=128,
    )
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})

    fake = _FakeRuntime()
    captured = {}

    def _capture(cfg, **_kwargs):
        captured["cfg"] = cfg
        return True

    monkeypatch.setattr(runtime_registry_module, "_send_icecast_metadata", _capture)
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(
        1,
        input_uri="C:/music/fallback.mp3",
        stream_title="Runtime Song",
        stream_artist="Runtime Artist",
        track_type="music",
    )
    status = reg.status(1)
    assert status["running"] is True
    assert status["required_outputs"] == {"icecast": True, "local": True}
    assert fake.last_cfg.icecast_mount == "/station1"
    assert fake.last_cfg.icecast_enabled is True
    assert fake.last_cfg.stream_codec_profile == "mp3_128"
    assert fake.last_cfg.stream_bitrate_kbps == 128
    assert fake.last_cfg.stream_title == "Runtime Song"
    assert fake.last_cfg.stream_artist == "Runtime Artist"
    assert fake.last_cfg.track_type == "music"
    assert float(fake.last_cfg.crossfade_seconds) == 3.0
    assert captured["cfg"].stream_title == "Runtime Song"
    assert captured["cfg"].stream_artist == "Runtime Artist"

    reg.stop_station(1)
    status2 = reg.status(1)
    assert status2["running"] is False
    assert status2["required_outputs"] == {"icecast": True, "local": True}


def test_registry_sets_required_outputs_from_disabled_icecast(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=2,
        local_output_enabled=True,
        output_device_id="dev2",
        icecast_enabled=False,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station2",
        icecast_user="source",
        icecast_password="hackme",
        output_gain_db=0.0,
    )

    fake = _FakeRuntime()
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(2, input_uri="C:/music/fallback.mp3")
    status = reg.status(2)
    assert status["required_outputs"] == {"icecast": False, "local": True}
    assert fake.last_cfg.icecast_enabled is False


def test_registry_recovers_failed_required_output_with_preserved_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_DISABLE_ICECAST_METADATA", "1")
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=True,
        output_device_id="dev1",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="test-password",
    )
    conn.close()

    fake = _RecoveringRuntime()
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/fallback.mp3")

    assert reg.required_outputs_healthy(1) is False
    status = reg.recover_station(1)

    assert fake.recover_calls == 1
    assert reg.required_outputs_healthy(1) is True
    assert status["recovery"]["state"] == "recovered"
    assert status["recovery"]["attempt_count"] == 1


def test_registry_recovery_uses_bounded_retry_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_DISABLE_ICECAST_METADATA", "1")
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=True,
        output_device_id="dev1",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="test-password",
    )
    conn.close()

    fake = _RecoveringRuntime(fail_recovery=True)
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/fallback.mp3")

    first = reg.recover_station(1)
    second = reg.recover_station(1)

    assert fake.recover_calls == 1
    assert first["recovery"]["state"] == "retry_wait"
    assert first["recovery"]["error_code"] == "origin_unreachable"
    assert 0 < first["recovery"]["retry_in_seconds"] <= 1.0
    assert second["recovery"]["attempt_count"] == 1


def test_registry_does_not_restart_flowing_source_for_probe_miss(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_DISABLE_ICECAST_METADATA", "1")
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=11154,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="test-password",
    )
    conn.close()

    fake = _FlowingUnverifiedRuntime()
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/fallback.mp3")

    status = reg.recover_station(1)

    assert fake.recover_calls == 0
    assert status["recovery"]["state"] == "monitoring"
    assert status["recovery"]["error_code"] == "output_unverified"
    assert status["recovery"]["retry_in_seconds"] > 0


def test_registry_start_creates_default_output_settings_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    fake = _FakeRuntime()
    monkeypatch.setattr(runtime_registry_module, "_send_icecast_metadata", lambda cfg: False)

    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/default.mp3")

    conn = get_connection()
    row = StationOutputRepository(conn).get(1)
    assert row is not None
    assert fake.last_cfg.icecast_enabled is False
    assert fake.last_cfg.local_output_enabled is True
    assert fake.last_cfg.output_device_id == ""
    assert int(row["local_output_enabled"]) == 1
    assert int(row["icecast_enabled"]) == 0


def test_service_policy_disables_local_playback_without_disabling_icecast(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_DISABLE_LOCAL_PLAYBACK", "1")
    monkeypatch.setenv("CLEANROOM_DISABLE_ICECAST_METADATA", "1")
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=True,
        output_device_id="default",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=11154,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="test-password",
    )
    conn.close()

    fake = _FakeRuntime()
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/default.mp3")

    assert fake.last_cfg.icecast_enabled is True
    assert fake.last_cfg.local_output_enabled is False


def test_station_setting_suppresses_primary_stream_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO stations (id, name) VALUES (10, 'RadioTEDU Situation Room')")
    StationOutputRepository(conn).upsert(
        station_id=10,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=11154,
        icecast_mount="/situation",
        icecast_user="source",
        icecast_password="test-password",
        stream_codec_profile="aac_low_192",
        stream_bitrate_kbps=192,
    )
    SettingsRepository(conn).upsert_station(10, {"metadata_suppressed": "true"})
    conn.close()

    fake = _FakeRuntime()
    captured = {"called": False}

    def _capture(_cfg, **_kwargs):
        captured["called"] = True
        return True

    monkeypatch.setattr(runtime_registry_module, "_send_icecast_metadata", _capture)
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(
        10,
        input_uri="C:/music/situation.mp3",
        stream_title="Private title",
        stream_artist="Private artist",
    )

    assert fake.last_cfg.metadata_suppressed is True
    # The registry still schedules its metadata worker, but the real sender and
    # FFmpeg command builder both honor this flag and emit nothing publicly.
    assert captured["called"] is True


def test_now_playing_hides_standalone_recording_placeholder():
    assert runtime_registry_module._compose_now_playing(
        "Song", "Artist", "[standalone recordings]"
    ) == "Artist - Song"
    assert runtime_registry_module._compose_now_playing(
        "Song", "Artist", "Real Album"
    ) == "Artist - Song (Real Album)"


def test_registry_hot_refreshes_quality_outputs_without_restarting_primary(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_DISABLE_ICECAST_METADATA", "1")
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="test-password",
    )
    SettingsRepository(conn).upsert_system(
        {
            "station_1_extra_icecast_outputs": json.dumps(
                [
                    {
                        "enabled": True,
                        "quality": "low",
                        "icecast_mount": "/station1-low",
                        "stream_codec_profile": "opus_32",
                        "stream_bitrate_kbps": 32,
                    }
                ]
            )
        }
    )
    conn.close()
    fake = _FakeRuntime()
    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/default.mp3")

    conn = get_connection()
    SettingsRepository(conn).upsert_system(
        {"station_1_extra_icecast_outputs": "[]"}
    )
    conn.close()
    result = reg.refresh_output_settings(1)

    assert result["producer_preserved"] is True
    assert fake.refreshed_outputs == ()
    assert fake.started is True
    assert not any(
        branch.startswith("icecast:")
        for branch in reg._required_outputs[1]
    )


def test_registry_start_self_heals_legacy_implicit_icecast_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="hackme",
        output_gain_db=0.0,
        stream_codec_profile="opus_96",
        stream_bitrate_kbps=96,
    )

    fake = _FakeRuntime()
    monkeypatch.setattr(runtime_registry_module, "_send_icecast_metadata", lambda cfg: False)

    reg = StationRuntimeRegistry(runtime_factory=lambda: fake)
    reg.start_station(1, input_uri="C:/music/default.mp3")

    row = StationOutputRepository(conn).get(1)
    assert row is not None
    assert fake.last_cfg.icecast_enabled is False
    assert fake.last_cfg.local_output_enabled is True
    assert int(row["local_output_enabled"]) == 1
    assert int(row["icecast_enabled"]) == 0


def test_send_icecast_metadata_posts_admin_update(monkeypatch):
    captured = {}

    class _DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return _DummyResponse()

    monkeypatch.setattr(runtime_registry_module, "urlopen", _fake_urlopen)
    cfg = StationPipelineConfig(
        input_uri="C:/music/demo.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/live",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        stream_title="Song X",
        stream_artist="Artist Y",
    )
    ok = runtime_registry_module._send_icecast_metadata(cfg)
    assert ok is True
    assert "/admin/metadata" in captured["url"]
    assert "mode=updinfo" in captured["url"]
    assert "mount=/live" in captured["url"]
    assert "Artist%20Y%20-%20Song%20X" in captured["url"]
    assert str(captured["auth"]).startswith("Basic ")
    assert int(captured["timeout"]) == 1


def test_send_icecast_metadata_skips_when_song_empty(monkeypatch):
    called = {"value": False}

    def _fake_urlopen(*args, **kwargs):
        called["value"] = True
        raise AssertionError("urlopen should not be called for empty metadata")

    monkeypatch.setattr(runtime_registry_module, "urlopen", _fake_urlopen)
    cfg = StationPipelineConfig(
        input_uri="C:/music/demo.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/live",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        stream_title="",
        stream_artist="",
    )
    ok = runtime_registry_module._send_icecast_metadata(cfg)
    assert ok is False
    assert called["value"] is False


def test_send_icecast_metadata_retries_transient_failure(monkeypatch):
    attempts = {"count": 0}

    class _DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _flaky_urlopen(request, timeout=0):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary failure")
        return _DummyResponse()

    monkeypatch.setattr(runtime_registry_module, "urlopen", _flaky_urlopen)
    cfg = StationPipelineConfig(
        input_uri="C:/music/demo.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/live",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        stream_title="Song Z",
        stream_artist="Artist Z",
    )
    ok = runtime_registry_module._send_icecast_metadata(cfg)
    assert ok is True
    assert attempts["count"] == 3


def test_send_icecast_metadata_fails_after_retry_budget(monkeypatch):
    attempts = {"count": 0}

    def _always_fails(*args, **kwargs):
        attempts["count"] += 1
        raise RuntimeError("permanent failure")

    monkeypatch.setattr(runtime_registry_module, "urlopen", _always_fails)
    cfg = StationPipelineConfig(
        input_uri="C:/music/demo.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/live",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        stream_title="Song W",
        stream_artist="Artist W",
    )
    ok = runtime_registry_module._send_icecast_metadata(cfg)
    assert ok is False
    assert attempts["count"] == 4


def test_registry_stop_all_stops_and_clears():
    reg = StationRuntimeRegistry(runtime_factory=lambda: _FakeRuntime())
    reg._runtimes[1] = _FakeRuntime()
    reg._runtimes[2] = _FakeRuntime()
    reg._required_outputs[1] = {"icecast": True, "local": False}
    reg._required_outputs[2] = {"icecast": False, "local": True}

    summary = reg.stop_all()
    assert set(summary["stations"]) == {1, 2}
    assert int(summary["stopped"]) == 2
    assert reg._runtimes == {}
    assert reg._required_outputs == {}
