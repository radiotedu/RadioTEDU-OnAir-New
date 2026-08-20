import threading
import time

import app.engine.worker_loop as worker_loop_module
from app.engine.worker_loop import StationWorkerLoopManager


class _FakeRuntimeRegistry:
    def is_process_running(self, station_id: int) -> bool:
        return False


class _FakeWorker:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def process_once(self):
        return {"source": "none", "reason": "test-loop"}


def test_worker_loop_manager_start_ticks_and_stop():
    mgr = StationWorkerLoopManager(
        runtime_registry=_FakeRuntimeRegistry(),
        worker_factory=lambda **kwargs: _FakeWorker(**kwargs),
    )

    start = mgr.start(station_id=1, fallback_uri="C:/music/fallback.mp3", interval_sec=0.05)
    assert start["running"] is True

    deadline = time.time() + 1.0
    ticks = 0
    while time.time() < deadline:
        status = mgr.status(1)
        ticks = int(status["ticks"])
        if ticks >= 1:
            break
        time.sleep(0.05)
    assert ticks >= 1

    stop = mgr.stop(1)
    assert stop["running"] is False
    assert stop["stopping"] is False

    restarted = mgr.start(
        station_id=1,
        fallback_uri="C:/music/fallback.mp3",
        interval_sec=0.05,
    )
    assert restarted["running"] is True
    mgr.stop(1)


def test_worker_loop_manager_stop_all():
    mgr = StationWorkerLoopManager(
        runtime_registry=_FakeRuntimeRegistry(),
        worker_factory=lambda **kwargs: _FakeWorker(**kwargs),
    )
    mgr.start(station_id=1, fallback_uri="", interval_sec=0.05)
    mgr.start(station_id=2, fallback_uri="", interval_sec=0.05)

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if int(mgr.status(1)["ticks"]) >= 1 and int(mgr.status(2)["ticks"]) >= 1:
            break
        time.sleep(0.05)

    summary = mgr.stop_all()
    assert set(summary["stations"]) == {1, 2}
    assert int(summary["stopped"]) == 2
    assert mgr.status(1)["running"] is False
    assert mgr.status(2)["running"] is False


def test_failure_backoff_recovers_quickly_before_escalating():
    assert worker_loop_module._failure_backoff_seconds(1, 1.0) == 1.0
    assert worker_loop_module._failure_backoff_seconds(2, 1.0) == 2.0
    assert worker_loop_module._failure_backoff_seconds(3, 1.0) == 5.0
    assert worker_loop_module._failure_backoff_seconds(6, 1.0) == 60.0
    assert worker_loop_module._failure_backoff_seconds(20, 1.0) == 60.0


def test_worker_loop_manager_updates_running_loop_config():
    mgr = StationWorkerLoopManager(
        runtime_registry=_FakeRuntimeRegistry(),
        worker_factory=lambda **kwargs: _FakeWorker(**kwargs),
    )

    mgr.start(station_id=1, fallback_uri="C:/music/a.mp3", interval_sec=0.05)
    updated = mgr.start(station_id=1, fallback_uri="C:/music/b.mp3", interval_sec=0.2)

    assert updated["running"] is True
    assert updated["fallback_uri"] == "C:/music/b.mp3"
    assert updated["interval_sec"] == 0.2

    mgr.stop(1)


def test_worker_loop_stop_never_reports_stopped_while_tick_is_alive(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class _BlockingWorker(_FakeWorker):
        def process_once(self):
            entered.set()
            release.wait(10.0)
            return {"source": "none", "reason": "released"}

    mgr = StationWorkerLoopManager(
        runtime_registry=_FakeRuntimeRegistry(),
        worker_factory=lambda **kwargs: _BlockingWorker(**kwargs),
    )
    monkeypatch.setattr(worker_loop_module, "_STOP_JOIN_TIMEOUT_SECONDS", 0.01)
    mgr.start(station_id=3, fallback_uri="", interval_sec=0.05)
    assert entered.wait(0.5)

    monkeypatch.setattr(worker_loop_module, "_WORKER_TICK_STALL_SECONDS", 0.01)
    deadline = time.time() + 0.5
    status = mgr.status(3)
    while not status["stalled"] and time.time() < deadline:
        time.sleep(0.01)
        status = mgr.status(3)
    assert status["tick_in_progress"] is True
    assert status["stalled"] is True
    assert status["tick_elapsed_seconds"] >= 0.01

    stopping = mgr.stop(3)

    assert stopping["running"] is True
    assert stopping["stopping"] is True
    release.set()
    deadline = time.time() + 1.0
    while mgr.status(3)["running"] and time.time() < deadline:
        time.sleep(0.01)
    assert mgr.stop(3)["running"] is False


def test_different_managers_use_different_lease_owner_ids():
    first = StationWorkerLoopManager(runtime_registry=_FakeRuntimeRegistry())
    second = StationWorkerLoopManager(runtime_registry=_FakeRuntimeRegistry())

    assert first._owner_id != second._owner_id
