from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.engine.station_worker import StationWorker


class _ScheduleRepo:
    def __init__(self, item, *, defer_count=1):
        self.item = item
        self.defer_count = defer_count
        self.deferred = []
        self.failed = []

    def current_playing(self, _station_id):
        return self.item

    def defer(self, item_id, *, retry_after_seconds, error=""):
        self.deferred.append((int(item_id), retry_after_seconds, error))
        if self.defer_count:
            self.item = None
        return self.defer_count

    def mark_failed(self, item_id):
        self.failed.append(int(item_id))


def _worker(*, defer_count=1):
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=20)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    item = {"id": 81, "track_id": 902, "playout_started_at": started_at}
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.schedule_repo = _ScheduleRepo(item, defer_count=defer_count)
    worker.runtime_registry = SimpleNamespace(status=lambda _station_id: {})
    worker._track_runtime_fields = lambda _track_id: (
        "E:/Programmes/recording.mp3",
        "Recording",
        "Host",
        "",
        "schedule",
    )
    worker._runtime_source_finished_naturally = lambda *_args: False
    worker._runtime_playback_alive = lambda _status: False
    worker._runtime_playback_matches = lambda *_args: False
    worker._restart_attempt_allowed = lambda *_args: (False, "retry_deferred")
    worker._set_playout_state = lambda *_args, **_kwargs: None
    worker._broadcast_worker_state = lambda **_kwargs: None
    return worker


def test_schedule_restart_limit_defers_row_without_consuming_it():
    worker = _worker()

    should_keep_schedule_owner = worker._advance_playing_schedule_item()

    assert should_keep_schedule_owner is False
    assert worker.schedule_repo.item is None
    assert len(worker.schedule_repo.deferred) == 1
    item_id, retry_after, error = worker.schedule_repo.deferred[0]
    assert item_id == 81
    assert retry_after > 0
    assert "runtime_mismatch" in error
    assert worker.schedule_repo.failed == []


def test_schedule_keeps_playout_ownership_if_retry_cannot_be_persisted():
    worker = _worker(defer_count=0)

    should_keep_schedule_owner = worker._advance_playing_schedule_item()

    assert should_keep_schedule_owner is True
    assert worker.schedule_repo.item is not None
    assert worker.schedule_repo.failed == []


def test_schedule_keeps_ownership_while_encoder_input_fifos_drain():
    worker = _worker()
    status = {
        "program_running": False,
        "producer_eof": False,
        "producer_draining": True,
        "active_input_uri": "E:/Programmes/recording.mp3",
    }
    worker.runtime_registry = SimpleNamespace(status=lambda _station_id: status)
    started = []
    worker._start_runtime_station = lambda *args, **kwargs: started.append(
        (args, kwargs)
    )

    should_keep_schedule_owner = worker._advance_playing_schedule_item()

    assert should_keep_schedule_owner is True
    assert worker.schedule_repo.item is not None
    assert worker.schedule_repo.failed == []
    assert started == []
