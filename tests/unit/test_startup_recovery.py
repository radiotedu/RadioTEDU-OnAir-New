import sqlite3
import threading

from app.services.startup_recovery import recover_station_startup


class Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Stop:
    def __init__(self, cancel_on_wait=False):
        self.cancelled = False
        self.cancel_on_wait = cancel_on_wait
        self.waits = []

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.waits.append(seconds)
        self.cancelled = self.cancel_on_wait
        return self.cancelled


def test_locked_database_at_boot_recovers_with_a_fresh_connection():
    connections = []
    attempts = []
    started = []

    def connect():
        attempts.append(True)
        if len(attempts) <= 2:
            raise sqlite3.OperationalError("database is locked")
        conn = Connection()
        connections.append(conn)
        return conn

    def start(conn, pending, stop):
        started.append(pending)
        return set()

    recover_station_startup(connect, start, Stop())
    assert len(attempts) == 3
    assert started == [None]
    assert all(conn.closed for conn in connections)


def test_failed_channel_is_retried_without_restarting_healthy_siblings():
    connections = []
    selections = []

    def connect():
        conn = Connection()
        connections.append(conn)
        return conn

    def start(conn, pending, stop):
        selections.append(pending)
        return {2} if pending is None else set()

    recover_station_startup(connect, start, Stop())
    assert selections == [None, {2}]
    assert len(connections) == 2
    assert all(conn.closed for conn in connections)


def test_a_late_media_volume_can_recover_after_many_failed_attempts():
    calls = []
    stop = Stop()

    def start(conn, pending, stop):
        calls.append(pending)
        return {8} if len(calls) < 12 else set()

    recover_station_startup(Connection, start, stop)
    assert len(calls) == 12
    assert calls[1:] == [{8}] * 11
    assert max(stop.waits) <= 30


def test_callback_database_error_keeps_the_failed_station_selection():
    calls = []

    def start(conn, pending, stop):
        calls.append(pending)
        if len(calls) == 1:
            return {5}
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return set()

    recover_station_startup(Connection, start, Stop())
    assert calls == [None, {5}, {5}]


def test_shutdown_interrupts_backoff_and_prevents_further_starts():
    calls = []
    stop = Stop(cancel_on_wait=True)
    recover_station_startup(
        Connection,
        lambda conn, pending, event: calls.append(pending) or {11},
        stop,
    )
    assert calls == [None]
    assert len(stop.waits) == 1


def test_shutdown_before_connection_does_not_open_the_database():
    stop = threading.Event()
    stop.set()

    def unexpected(*args):
        raise AssertionError("Shutdown must not acquire a connection or start a worker")

    recover_station_startup(unexpected, unexpected, stop)


def test_shutdown_during_connection_acquisition_closes_without_starting():
    stop = threading.Event()
    conn = Connection()

    def connect():
        stop.set()
        return conn

    def unexpected(*args):
        raise AssertionError("Worker started after shutdown")

    recover_station_startup(connect, unexpected, stop)
    assert conn.closed


def test_completed_boot_task_does_not_supervise_an_intentional_later_stop():
    calls = []
    stop = Stop()
    recover_station_startup(
        Connection,
        lambda conn, pending, event: calls.append(pending) or set(),
        stop,
    )
    assert calls == [None]
    assert stop.waits == []
