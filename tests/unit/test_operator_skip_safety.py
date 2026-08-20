import threading

import pytest
from fastapi import HTTPException

from app.api import runtime
from app.engine import worker_loop


class _LoopManager:
    def __init__(self, *, resume=True):
        self.running = True
        self.resume = resume
        self.calls = []

    def status(self, _station_id):
        return {
            "running": self.running,
            "fallback_uri": "file:///continuity.mp3",
            "interval_sec": 1.0,
        }

    def stop(self, station_id):
        self.calls.append(("stop", station_id))
        self.running = False
        return self.status(station_id)

    def start(self, station_id, **_kwargs):
        self.calls.append(("start", station_id))
        self.running = self.resume
        return self.status(station_id)


class _Runtime:
    def __init__(self):
        self.running = True
        self.calls = []

    def status(self, _station_id):
        return {
            "running": self.running,
            "active_input_uri": "file:///current.mp3",
            "active_stream_title": "Current",
            "active_stream_artist": "Artist",
            "active_track_type": "music",
        }

    def is_process_running(self, _station_id):
        return self.running

    def stop_station(self, station_id):
        self.calls.append(("stop", station_id))
        self.running = False

    def start_station(self, station_id, **kwargs):
        self.calls.append(("start", station_id, kwargs["input_uri"]))
        self.running = True


class _Connection:
    def close(self):
        return None


class _Repo:
    begin_result = "ok"
    commit_error = None
    commits = 0

    def __init__(self, _connection):
        self.rolled_back = False

    def begin_skip_playing_item(self, **_kwargs):
        return self.begin_result

    def commit_skip_playing_item(self, **_kwargs):
        if self.commit_error:
            raise self.commit_error
        type(self).commits += 1
        return "ok"

    def rollback_skip_playing_item(self):
        self.rolled_back = True


def _wire_skip(monkeypatch, *, resume=True):
    loop = _LoopManager(resume=resume)
    registry = _Runtime()
    _Repo.begin_result = "ok"
    _Repo.commit_error = None
    _Repo.commits = 0
    monkeypatch.setattr(runtime, "worker_loop_manager", loop)
    monkeypatch.setattr(runtime, "runtime_registry", registry)
    monkeypatch.setattr(runtime, "get_connection", _Connection)
    monkeypatch.setattr(runtime, "QueueRepository", _Repo)
    monkeypatch.setattr(runtime, "init_db", lambda: None)
    monkeypatch.setattr(runtime, "_broadcast_runtime_events", lambda *_args, **_kwargs: None)
    return loop, registry


def test_skip_stale_validation_resumes_without_stopping_runtime(monkeypatch):
    loop, registry = _wire_skip(monkeypatch)
    _Repo.begin_result = "stale"

    with pytest.raises(HTTPException) as exc:
        runtime.operator_skip_current(1, runtime.OperatorSkipPayload(item_id=10, expected_revision="stale"))

    assert exc.value.status_code == 409
    assert registry.calls == []
    assert loop.calls == [("stop", 1), ("start", 1)]


def test_skip_commit_failure_restores_prior_runtime_before_resuming(monkeypatch):
    loop, registry = _wire_skip(monkeypatch)
    _Repo.commit_error = RuntimeError("injected sqlite failure")

    with pytest.raises(HTTPException) as exc:
        runtime.operator_skip_current(1, runtime.OperatorSkipPayload(item_id=10, expected_revision="r1"))

    assert exc.value.status_code == 503
    assert registry.calls == [("stop", 1), ("start", 1, "file:///current.mp3")]
    assert loop.calls == [("stop", 1), ("start", 1)]


def test_skip_resume_failure_leaves_committed_skip_safely_stopped(monkeypatch):
    loop, registry = _wire_skip(monkeypatch, resume=False)

    with pytest.raises(HTTPException) as exc:
        runtime.operator_skip_current(1, runtime.OperatorSkipPayload(item_id=10, expected_revision="r1"))

    assert exc.value.status_code == 503
    assert _Repo.commits == 1
    assert registry.calls == [("stop", 1)]
    assert loop.running is False


def test_operator_playout_operations_are_serialized_per_station():
    order = []
    entered = threading.Event()
    release = threading.Event()

    def first():
        with runtime._serialized_playout_operation(7) as generation:
            order.append(("first", generation))
            entered.set()
            release.wait(timeout=2)

    def second():
        entered.wait(timeout=2)
        with runtime._serialized_playout_operation(7) as generation:
            order.append(("second", generation))

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    entered.wait(timeout=2)
    assert order == [("first", 1)]
    release.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert order == [("first", 1), ("second", 2)]


def test_worker_acknowledges_sequence_seen_before_evaluation(monkeypatch):
    stop_event = threading.Event()

    class _Queue:
        sequence = 4

        def change_sequence(self, _station_id):
            return self.sequence

    class _Worker:
        def __init__(self):
            self.queue_repo = _Queue()

        def process_once(self):
            self.queue_repo.sequence = 5  # committed after this tick began
            stop_event.set()
            return {"ok": True}

        def close(self):
            return None

    class _Registry:
        def is_process_running(self, _station_id):
            return False

    manager = worker_loop.StationWorkerLoopManager(
        runtime_registry=_Registry(), runtime_supervisor=object(), worker_factory=lambda **_kwargs: _Worker()
    )
    monkeypatch.setattr(manager, "_recover_runtime_if_needed", lambda *_args: None)
    manager._loops[1] = {
        "stop_event": stop_event,
        "interval_sec": 0.01,
        "fallback_uri": "",
        "next_attempt_monotonic": 0.0,
        "ticks": 0,
        "last_result": None,
        "last_error": "",
        "failure_count": 0,
        "last_backoff_seconds": 0.0,
        "last_observed_queue_sequence": 0,
    }

    manager._loop_entry(1)

    assert manager._loops[1]["last_observed_queue_sequence"] == 4
