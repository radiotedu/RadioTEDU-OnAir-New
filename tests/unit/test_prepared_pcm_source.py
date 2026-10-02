import io
import threading
import time

from app.audio.prepared_pcm_source import PreparedPCMSource


class Process:
    def __init__(self, pcm, *, code=None):
        self.stdout = io.BytesIO(pcm)
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code or 0

    def terminate(self):
        self.terminated = True
        self.code = -1


def wait_ready(prepared):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = prepared.snapshot()
        if state["ready"] or state["failed"] or state["cancelled"]:
            return state
        time.sleep(.002)
    raise AssertionError("preparation did not finish")


def prepare(process, *, key="next", capacity=4096, timeout=1):
    return PreparedPCMSource(key, lambda: ["ffmpeg"], lambda *a, **k: process,
                             lambda p: p.terminate(), capacity_bytes=capacity,
                             timeout_seconds=timeout)


def test_prefix_transfers_once_and_preserves_every_sample():
    pcm = bytes(range(256)) * 40
    process = Process(pcm)
    prepared = prepare(process)
    assert wait_ready(prepared)["buffered_pcm_bytes"] == 4096
    assert prepared.take("different") is None
    assert prepared.take("next") is process
    assert prepared.take("next") is None
    received = bytearray()
    while chunk := process.stdout.read(777):
        received.extend(chunk)
    assert bytes(received) == pcm
    prepared.cancel()
    assert not process.terminated


def test_short_clean_clip_is_prepared_in_full():
    pcm = b"\x01\x00\x02\x00" * 256
    process = Process(pcm, code=0)
    prepared = prepare(process)
    assert wait_ready(prepared)["ready"]
    assert prepared.take("next").stdout.read() == pcm


def test_failed_decoder_cannot_publish_a_partial_advertisement():
    process = Process(b"ad" * 100, code=1)
    prepared = prepare(process)
    assert wait_ready(prepared)["failed"]
    assert prepared.take("next") is None


def test_empty_clip_is_never_ready():
    prepared = prepare(Process(b"", code=0))
    assert wait_ready(prepared)["failed"]
    assert prepared.take("next") is None


def test_cancel_terminates_unused_decoder_and_releases_buffer():
    process = Process(b"a" * 8192)
    prepared = prepare(process)
    assert wait_ready(prepared)["ready"]
    prepared.cancel()
    assert process.terminated and process.stdout.closed
    assert prepared.snapshot()["buffered_pcm_bytes"] == 0
    assert prepared.take("next") is None


def test_timeout_cancels_ready_source_that_is_never_used():
    process = Process(b"a" * 8192)
    prepared = prepare(process, timeout=.05)
    assert wait_ready(prepared)["ready"]
    prepared._timer.join(timeout=1)
    assert process.terminated
    assert prepared.take("next") is None


def test_cancel_during_command_build_does_not_spawn_a_late_process():
    entered = threading.Event()
    resume = threading.Event()
    spawned = []

    def command():
        entered.set()
        resume.wait(1)
        return ["ffmpeg"]

    prepared = PreparedPCMSource("key", command,
                                 lambda *a, **k: spawned.append(True), lambda p: None)
    assert entered.wait(1)
    prepared.cancel()
    resume.set()
    prepared._thread.join(1)
    assert spawned == []


def test_cancel_racing_spawn_reaps_the_late_process():
    entered = threading.Event()
    resume = threading.Event()
    process = Process(b"a" * 8192)

    def spawn(*a, **k):
        entered.set()
        resume.wait(1)
        return process

    prepared = PreparedPCMSource("key", lambda: ["ffmpeg"], spawn,
                                 lambda p: p.terminate())
    assert entered.wait(1)
    prepared.cancel()
    resume.set()
    prepared._thread.join(1)
    assert process.terminated
