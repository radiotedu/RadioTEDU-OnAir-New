import copy
import threading

import pytest

from app.engine.worker_heartbeat import WorkerHeartbeatPublisher


def publisher_fixture():
    now, writes = [0.0], []

    def writer(config, payload, **kwargs):
        writes.append(copy.deepcopy((config, payload, kwargs)))

    publisher = WorkerHeartbeatPublisher({}, writer, clock=lambda: now[0])
    payload = {"event": "tick", "last_result": {"source": "playing", "reason": "track_in_progress"}}
    status = {"active_input_uri": "song.flac", "program_running": True}
    return publisher, now, writes, payload, status


def test_routine_ticks_and_liveness_share_one_write_budget():
    publisher, now, writes, payload, status = publisher_fixture()
    for tick in range(600):
        now[0] = tick / 10
        payload["ticks"] = tick
        payload["tick_duration_seconds"] = tick / 10000
        status["elapsed"] = tick / 10
        publisher.publish(payload, runtime_status=status)
        if tick % 10 == 0:
            publisher.publish({**payload, "event": "liveness"}, runtime_status=status)
    assert len(writes) == 60
    assert writes[-1][1]["ticks"] == 590


@pytest.mark.parametrize("field,value", [
    ("active_input_uri", "next.flac"), ("program_running", False),
    ("producer_eof", True), ("producer_draining", True),
    ("producer_finalizing", True), ("transition_active", True),
    ("live_mic_active", True),
])
def test_programme_phase_changes_are_immediate(field, value):
    publisher, now, writes, payload, status = publisher_fixture()
    assert publisher.publish(payload, runtime_status=status)
    now[0] = 0.01
    status[field] = value
    assert publisher.publish(payload, runtime_status=status)
    assert len(writes) == 2


@pytest.mark.parametrize("field,value", [
    ("last_error", "RuntimeError"), ("failure_count", 1),
    ("scheduler_stalled", True), ("runtime_status_available", False),
])
def test_fault_changes_are_immediate(field, value):
    publisher, now, writes, payload, status = publisher_fixture()
    publisher.publish(payload, runtime_status=status)
    now[0] = 0.01
    payload[field] = value
    assert publisher.publish(payload, runtime_status=status)
    assert len(writes) == 2


@pytest.mark.parametrize("extra", [False, True])
def test_source_acceptance_changes_are_immediate(extra):
    publisher, now, writes, payload, status = publisher_fixture()
    health = {"source_response_confirmed": True}
    if extra:
        status["extra_icecast_mounts"] = [{"mount": "/low", "health": health}]
    else:
        status["icecast_mount_health"] = health
    publisher.publish(payload, runtime_status=status)
    now[0] = 0.01
    health["source_response_confirmed"] = False
    assert publisher.publish(payload, runtime_status=status)
    assert len(writes) == 2


def test_ready_and_stopped_are_always_written():
    publisher, now, writes, payload, status = publisher_fixture()
    publisher.publish(payload, runtime_status=status)
    assert publisher.publish({"event": "ready"}, runtime_status=status)
    assert publisher.publish({"event": "stopped"}, runtime_status=status, running=False)
    assert writes[-1][2]["running"] is False


def test_failed_write_is_retried_without_waiting():
    publisher, now, writes, payload, status = publisher_fixture()
    normal_writer = publisher._writer

    def failing_writer(*args, **kwargs):
        raise OSError("file sharing race")

    publisher._writer = failing_writer
    with pytest.raises(OSError):
        publisher.publish(payload, runtime_status=status)
    publisher._writer = normal_writer
    assert publisher.publish(payload, runtime_status=status)
    assert len(writes) == 1


def test_concurrent_scheduler_and_liveness_coalesce():
    publisher, now, writes, payload, status = publisher_fixture()
    barrier = threading.Barrier(8)

    def emit():
        barrier.wait()
        publisher.publish(payload, runtime_status=status)

    threads = [threading.Thread(target=emit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert len(writes) == 1
