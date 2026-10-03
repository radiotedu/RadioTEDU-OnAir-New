from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.engine.station_worker import StationWorker


def make_worker(*, track_type="jingle", duration=8, elapsed=6, expected="C:/music/owned.mp3", active=None):
    playing = {"id": 17, "track_id": 91, "duration": duration, "track_type": track_type,
               "started_at": (datetime.now(timezone.utc)-timedelta(seconds=elapsed)).strftime("%Y-%m-%d %H:%M:%S")}
    status = {"active_input_uri": active or expected, "program_running": False,
              "producer_eof": False, "producer_draining": True, "running": True,
              "producer_exit_code": 0}
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.queue_repo = SimpleNamespace(current_playing=lambda _: playing, next_pending=lambda _: None)
    worker.runtime_registry = SimpleNamespace(status=lambda _: dict(status))
    worker._track_runtime_fields = lambda _: (expected, "Owned", "Artist", "", track_type)
    calls = {"restarted": [], "completed": []}
    worker._restart_playing_queue_item_if_runtime_mismatched = lambda row, **kwargs: calls["restarted"].append(row["id"]) or True
    worker._complete_queue_item = lambda row: calls["completed"].append(row["id"])
    return worker, status, calls


@pytest.mark.parametrize("track_type,duration,elapsed", [
    ("jingle", 8, 6),
    ("music", 180, 176),
    ("music", 180, 181),
    ("music", 0, 60),
    ("announcement", 8, 7),
    ("podcast", 3600, 8000),
])
def test_matching_decoded_tail_never_restarts_or_consumes_owned_row(track_type, duration, elapsed):
    worker, status, calls = make_worker(track_type=track_type, duration=duration, elapsed=elapsed)
    assert worker._advance_playing_queue_item() is False
    assert calls == {"restarted": [], "completed": []}


def test_drain_for_different_file_does_not_mask_wrong_owned_source():
    worker, status, calls = make_worker(active="C:/music/other.mp3")
    assert worker._advance_playing_queue_item() is True
    assert calls == {"restarted": [17], "completed": []}


def test_failed_decoder_without_retained_tail_still_retries_same_row():
    worker, status, calls = make_worker()
    status.update(producer_draining=False, producer_exit_code=1)
    assert worker._advance_playing_queue_item() is True
    assert calls == {"restarted": [17], "completed": []}


def test_clean_eof_after_tail_drain_completes_once_without_restart():
    worker, status, calls = make_worker()
    status.update(producer_draining=False, producer_eof=True)
    assert worker._advance_playing_queue_item() is True
    assert calls == {"restarted": [], "completed": [17]}
