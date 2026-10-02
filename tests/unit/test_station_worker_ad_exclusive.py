import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.engine import station_worker as worker_module
from app.engine.station_worker import StationWorker


def _worker_with_active_ad():
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.worker_id = "test-worker"
    worker.runtime_registry = None
    worker.lease_service = SimpleNamespace(try_acquire=lambda *_: True)
    worker.ad_repo = SimpleNamespace(current_playing=lambda _station_id: {"id": 17})
    worker._get_active_show_session = lambda: None
    worker._process_show_lifecycle = lambda _session: None
    worker._maybe_insert_startup_sound = lambda: None
    worker._fail_cross_station_queue_items = lambda: None
    worker._fail_pending_items_without_media_reference = lambda: 0
    worker._remove_unplanned_pending_ads = lambda: None
    worker._autofill_queue = lambda: None
    worker._prefetch_upcoming_audio = lambda: None
    worker._get_sweeper_settings = lambda: {"enabled": False}
    worker._remove_pending_jingles = lambda: None
    worker._maybe_prepare_ai_queue = lambda: None
    worker._advance_playing_ad_item = lambda: False
    return worker


def test_active_ad_owns_playout_before_music_queue_advances():
    worker = _worker_with_active_ad()
    worker._advance_playing_queue_item = lambda: (_ for _ in ()).throw(
        AssertionError("music queue advanced while an ad was playing")
    )

    result = worker.process_once()

    assert result == {"source": "playing", "reason": "ad_in_progress", "item_id": 17}


def test_ad_is_not_failed_during_runtime_restart_cooldown():
    failed = []
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.runtime_registry = SimpleNamespace(status=lambda _station_id: {})
    worker.ad_repo = SimpleNamespace(mark_failed=failed.append)
    worker._ads_enabled = lambda: True
    worker._track_runtime_fields = lambda _track_id: (
        "E:/Ads/PowerAPP.mp3", "PowerAPP", "", "", "ad"
    )
    worker._runtime_source_finished_naturally = lambda *_args: False
    worker._runtime_playback_matches = lambda *_args: False
    worker._restart_attempt_allowed = lambda *_args: (False, "restart_cooldown_active")

    result = worker._restart_playing_ad_item_if_runtime_mismatched(
        {"id": 42, "track_id": 52944}
    )

    assert result is False
    assert failed == []


def test_ad_restart_limit_requests_a_persisted_retry(monkeypatch):
    state = {"attempts": 2, "next_allowed": 0.0, "reason": ""}
    monkeypatch.setattr(worker_module, "_MAX_RESTART_ATTEMPTS_PER_ITEM", 2)
    monkeypatch.setattr(worker_module, "_RESTART_COOLDOWN_SEC", 2.0)
    monkeypatch.setattr(
        worker_module,
        "_RESTART_SUPPRESSION",
        {(4, "ads", 42): state},
    )
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4

    allowed, reason = worker._restart_attempt_allowed("ads", 42)

    assert allowed is False
    assert reason == "retry_deferred"
    assert state["attempts"] == 0
    assert state["next_allowed"] > 0.0


def test_repeated_ad_restart_failure_releases_ownership_without_consuming_item(
    monkeypatch,
):
    playing = {"id": 42, "track_id": 52944}
    deferred = []
    done = []
    failed = []

    def defer(item_id, *, retry_after_seconds, error=""):
        deferred.append((item_id, retry_after_seconds, error))
        if int(item_id) == playing["id"]:
            playing.clear()
        return 1

    monkeypatch.setattr(
        worker_module,
        "_RESTART_SUPPRESSION",
        {
            (4, "ads", 42): {
                "attempts": worker_module._MAX_RESTART_ATTEMPTS_PER_ITEM,
                "next_allowed": 0.0,
                "reason": "",
            }
        },
    )
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.runtime_registry = SimpleNamespace(status=lambda _station_id: {})
    worker.ad_repo = SimpleNamespace(
        current_playing=lambda _station_id: playing,
        mark_done=done.append,
        mark_failed=failed.append,
        defer=defer,
    )
    worker._ads_enabled = lambda: True
    worker._track_runtime_fields = lambda _track_id: (
        "E:/Ads/PowerAPP.mp3", "PowerAPP", "", "", "ad"
    )
    worker._runtime_source_finished_naturally = lambda *_args: False
    worker._runtime_playback_matches = lambda *_args: False
    worker._set_playout_state = lambda *_args, **_kwargs: None
    worker._broadcast_worker_state = lambda **_kwargs: None

    result = worker._restart_playing_ad_item_if_runtime_mismatched(playing)

    assert result is True
    assert deferred and deferred[0][0] == 42
    assert deferred[0][1] > 0
    assert "runtime_mismatch" in deferred[0][2]
    assert playing == {}
    assert done == []
    assert failed == []


def _worker_with_playing_ad(*, runtime_status):
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.conn = None
    worker.runtime_registry = SimpleNamespace(status=lambda _station_id: runtime_status)
    playing = {
        "id": 42,
        "track_id": 52944,
        "started_at": (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "duration": 5.0,
    }
    worker.ad_repo = SimpleNamespace(
        current_playing=lambda _station_id: playing,
        mark_done=lambda _item_id: done.append(_item_id),
    )
    done = []
    worker._ads_enabled = lambda: True
    worker._track_runtime_fields = lambda _track_id: (
        "E:/Ads/PowerAPP.mp3",
        "PowerAPP",
        "",
        "",
        "ad",
    )
    worker._set_playout_state = lambda *_args, **_kwargs: None
    worker._broadcast_worker_state = lambda **_kwargs: None
    return worker, done


def test_ad_is_not_completed_from_duration_while_its_source_is_still_playing():
    worker, done = _worker_with_playing_ad(
        runtime_status={
            "running": True,
            "program_running": True,
            "producer_eof": False,
            "active_input_uri": "E:/Ads/PowerAPP.mp3",
        }
    )

    assert worker._advance_playing_ad_item() is False
    assert done == []


def test_ad_completes_at_clean_eof_while_sink_fifo_drains():
    worker, done = _worker_with_playing_ad(
        runtime_status={
            "running": True,
            "program_running": False,
            "producer_eof": True,
            "producer_draining": True,
            "active_input_uri": "E:/Ads/PowerAPP.mp3",
        }
    )

    assert worker._advance_playing_ad_item() is True
    assert done == [42]


def test_unplanned_ad_cleanup_commits_even_when_no_rows_match(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, track_type TEXT)")
    conn.execute(
        "CREATE TABLE queue_items (id INTEGER PRIMARY KEY, station_id INTEGER, "
        "status TEXT, dedupe_key TEXT, track_id INTEGER, finished_at TEXT)"
    )
    conn.commit()
    monkeypatch.setattr(worker_module, "station_ads_enabled", lambda *_args: False)
    monkeypatch.setattr(worker_module, "resolve_song_ad_plans", lambda *_args: [])
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.conn = conn

    assert worker._remove_unplanned_pending_ads() == 0
    assert conn.in_transaction is False
    conn.close()


def test_host_track_cannot_preempt_a_playing_song():
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.queue_repo = SimpleNamespace(
        current_playing=lambda _station_id: {"id": 3, "track_id": 9}
    )
    worker.runtime_registry = SimpleNamespace(
        start_station=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host playback must wait for the song boundary")
        )
    )

    result = worker._play_host_track(11, 12)

    assert result == {"source": "playing", "reason": "waiting_for_track_boundary"}


def test_clean_music_eof_advances_even_when_database_duration_is_stale():
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.runtime_registry = SimpleNamespace(
        status=lambda _station_id: {
            "running": False,
            "program_running": False,
            "producer_eof": True,
            "active_input_uri": "C:/music/complete.mp3",
        }
    )
    worker.queue_repo = SimpleNamespace(
        current_playing=lambda _station_id: {
            "id": 31,
            "track_id": 41,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "duration": 1800.0,
            "track_type": "music",
        },
        next_pending=lambda _station_id: None,
    )
    worker._track_runtime_fields = lambda _track_id: (
        "C:/music/complete.mp3", "Complete", "Artist", "", "music"
    )
    completed = []
    worker._complete_queue_item = completed.append

    assert worker._advance_playing_queue_item() is True
    assert len(completed) == 1
    assert completed[0]["id"] == 31


def test_unknown_duration_crash_retries_from_start_not_wall_clock_offset():
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.runtime_registry = SimpleNamespace(
        status=lambda _station_id: {
            "running": False,
            "program_running": False,
            "producer_eof": False,
            "active_input_uri": "C:/music/unknown.mp3",
        }
    )
    worker.queue_repo = SimpleNamespace(
        current_playing=lambda _station_id: {
            "id": 32,
            "track_id": 42,
            "started_at": (datetime.now(timezone.utc) - timedelta(seconds=50)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "duration": 0,
            "track_type": "music",
        },
        next_pending=lambda _station_id: None,
    )
    worker._track_runtime_fields = lambda _track_id: (
        "C:/music/unknown.mp3", "Unknown", "Artist", "", "music"
    )
    recovered = []
    worker._restart_playing_queue_item_if_runtime_mismatched = (
        lambda _item, *, start_offset_seconds: recovered.append(start_offset_seconds)
        or False
    )

    assert worker._advance_playing_queue_item() is False
    assert recovered == [0.0]


def test_explicit_dead_producer_overrides_healthy_output_branch():
    assert StationWorker._runtime_playback_alive(
        {
            "running": True,
            "program_running": False,
            "producer_eof": False,
            "branch_health": {"icecast": True},
            "required_outputs": {"icecast": True},
        }
    ) is False


def _host_worker(runtime_status):
    started = []
    popped = []
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.runtime_registry = SimpleNamespace(
        status=lambda _station_id: runtime_status,
        start_station=lambda *args, **kwargs: started.append((args, kwargs)),
    )
    worker.playout_state = SimpleNamespace(
        get_current=lambda _station_id: {"source": "host", "item_id": 19}
    )
    worker.program_queue_repo = SimpleNamespace(
        list_items=lambda _station_id: [{"id": 19, "track_id": 52944}],
        pop_item=popped.append,
    )
    worker._track_runtime_fields = lambda _track_id: (
        "E:/Host/show.mp3", "Host Track", "Presenter", "Show", "spoken_word"
    )
    worker._set_playout_state = lambda *_args, **_kwargs: None
    worker._broadcast_worker_state = lambda **_kwargs: None
    return worker, started, popped


def test_host_crash_with_healthy_sink_retries_same_item_from_start():
    worker, started, popped = _host_worker(
        {
            "running": True,
            "program_running": False,
            "producer_eof": False,
            "active_input_uri": "E:/Host/show.mp3",
            "branch_health": {"icecast": True},
            "required_outputs": {"icecast": True},
        }
    )

    assert worker._advance_host_track() is False
    assert popped == []
    assert len(started) == 1
    assert started[0][0][1] == "E:/Host/show.mp3"
    assert started[0][1]["start_offset_seconds"] == 0.0


def test_host_item_is_consumed_only_after_matching_clean_eof():
    worker, started, popped = _host_worker(
        {
            "running": True,
            "program_running": False,
            "producer_eof": True,
            "active_input_uri": "E:/Host/show.mp3",
        }
    )

    assert worker._advance_host_track() is True
    assert popped == [19]
    assert started == []


def test_host_item_stays_owned_while_encoder_input_fifos_drain():
    worker, started, popped = _host_worker(
        {
            "running": True,
            "program_running": False,
            "producer_eof": False,
            "producer_draining": True,
            "active_input_uri": "E:/Host/show.mp3",
        }
    )

    assert worker._advance_host_track() is False
    assert popped == []
    assert started == []


def test_host_item_releases_at_clean_eof_while_encoder_input_fifos_drain():
    worker, started, popped = _host_worker(
        {
            "running": True,
            "program_running": False,
            "producer_eof": True,
            "producer_draining": True,
            "active_input_uri": "E:/Host/show.mp3",
        }
    )

    assert worker._advance_host_track() is True
    assert popped == [19]
    assert started == []


def test_repeated_host_crash_releases_playout_without_consuming_item():
    worker, started, popped = _host_worker(
        {
            "running": True,
            "program_running": False,
            "producer_eof": False,
            "active_input_uri": "E:/Host/show.mp3",
            "branch_health": {"icecast": True},
            "required_outputs": {"icecast": True},
        }
    )
    states = []
    worker._host_retry_allowed = lambda _item_id: True
    worker._record_host_retry_failure = lambda _item_id: False
    worker._set_playout_state = lambda *args, **kwargs: states.append((args, kwargs))

    assert worker._advance_host_track() is True
    assert popped == []
    assert started == []
    assert states[-1][0] == ("none", None)
    assert states[-1][1]["reason"] == "host_retry_cooldown"


def test_continuity_fallback_helper_starts_configured_audio():
    started = []
    state = []
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.fallback_uri = "C:/radio/continuity.mp3"
    worker.runtime_registry = SimpleNamespace(
        start_station=lambda *args, **kwargs: started.append((args, kwargs))
    )
    worker._station_name = lambda: "Test FM"
    worker._fallback_title_from_uri = lambda _uri: "Continuity"
    worker._set_playout_state = lambda *args, **kwargs: state.append((args, kwargs))

    assert worker._start_continuity_fallback(
        reason="source_start_failed", failed_source="ads"
    ) is True
    assert len(started) == 1
    assert started[0][0][1] == worker.fallback_uri
    assert state


def test_ready_schedule_waits_for_the_current_song_boundary(monkeypatch):
    worker = _worker_with_active_ad()
    worker.ad_repo.current_playing = lambda _station_id: None
    worker._advance_playing_ad_item = lambda: False
    worker._advance_playing_queue_item = lambda: False
    worker._advance_playing_schedule_item = lambda: False
    worker._fail_disabled_active_ads = lambda: 0
    worker._ensure_hourly_ad_break = lambda: None
    worker._advance_host_track = lambda: None
    worker._next_due_ad_if_allowed = lambda: None
    worker._finish_playing_queue_item = lambda: (_ for _ in ()).throw(
        AssertionError("a scheduled item interrupted the current song")
    )
    worker._song_ad_refresh_due = None
    worker.fallback_uri = ""
    worker.queue_repo = SimpleNamespace(
        current_playing=lambda _station_id: {
            "id": 7,
            "track_id": 70,
            "track_type": "music",
        },
        next_pending=lambda _station_id: None,
    )
    worker.schedule_repo = SimpleNamespace(
        next_ready=lambda _station_id: {"id": 8, "track_id": 80}
    )
    worker.program_queue_repo = SimpleNamespace(get_source=lambda _station_id: "queue")
    worker.playout_state = SimpleNamespace(
        get_current=lambda _station_id: {"source": "none"}
    )
    source_args = {}

    def choose_source(**kwargs):
        source_args.update(kwargs)
        return "none"

    monkeypatch.setattr(worker_module, "choose_source", choose_source)
    monkeypatch.setattr(worker_module, "_song_ad_refresh_due", lambda *_args: False)

    result = worker.process_once()

    assert source_args["schedule_ready"] is False
    assert result == {"source": "playing", "reason": "track_in_progress"}
