from __future__ import annotations

import queue
import time
import unittest
from types import SimpleNamespace

from app.audio.icecast_audio_sink import (
    IcecastAudioSink,
    _mount_spread_seconds,
    _retry_spread_window_seconds,
)


class _Process:
    def __init__(self):
        self.stdin = object()

    @staticmethod
    def poll():
        return None


class _ControlledRetryStop:
    def __init__(self, stop_after: int):
        self.stop_after = int(stop_after)
        self.wait_calls = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.wait_calls.append(float(delay))
        if len(self.wait_calls) >= self.stop_after:
            self.stopped = True
            return True
        return False

    def set(self):
        self.stopped = True


class QualityBackpressureResyncTests(unittest.TestCase):
    def test_mount_reconnect_spread_is_deterministic_and_bounded(self):
        first = SimpleNamespace(
            icecast_host="stream.example",
            icecast_port=8000,
            icecast_mount="/lofi-low",
        )
        second = SimpleNamespace(
            icecast_host="stream.example",
            icecast_port=8000,
            icecast_mount="/classic-flac",
        )

        first_delay = _mount_spread_seconds(first, 2.0)

        self.assertEqual(first_delay, _mount_spread_seconds(first, 2.0))
        self.assertGreaterEqual(first_delay, 0.0)
        self.assertLessEqual(first_delay, 2.0)
        self.assertNotEqual(first_delay, _mount_spread_seconds(second, 2.0))

    def test_reconnect_spread_widens_as_backoff_grows(self):
        self.assertEqual(_retry_spread_window_seconds(1.0), 8.0)
        self.assertEqual(_retry_spread_window_seconds(15.0), 15.0)
        self.assertEqual(_retry_spread_window_seconds(30.0), 30.0)
        self.assertEqual(_retry_spread_window_seconds(90.0), 30.0)

    def test_initial_connect_spread_is_explicit_and_bounded(self):
        sink = IcecastAudioSink(
            "ffmpeg",
            lambda *_args, **_kwargs: None,
            initial_connect_spread_sec=30.0,
        )
        self.assertEqual(sink._initial_connect_spread_sec, 30.0)

    def test_source_write_health_replaces_listener_probe_when_disabled(self):
        sink = IcecastAudioSink("ffmpeg", lambda *_args, **_kwargs: None)
        sink._process = _Process()
        sink._last_network_write_monotonic = time.monotonic()
        self.assertIs(sink.health_snapshot()["mount_healthy"], True)

        sink._network_failed = True
        self.assertIs(sink.health_snapshot()["mount_healthy"], False)

    def test_full_branch_queue_resyncs_without_blocking_sibling_fanout(self):
        sink = IcecastAudioSink("ffmpeg", lambda *_args, **_kwargs: None)
        sink._process = _Process()
        old_chunks = 0
        while True:
            try:
                sink._pcm_queue.put_nowait(f"stale-{old_chunks}".encode())
                old_chunks += 1
            except queue.Full:
                break

        started = time.monotonic()
        accepted = sink.write_pcm(b"latest-program-clock")
        elapsed = time.monotonic() - started

        self.assertFalse(accepted)
        self.assertLess(elapsed, 0.05)
        retained = []
        while not sink._pcm_queue.empty():
            retained.append(sink._pcm_queue.get_nowait())
        self.assertEqual(len(retained), 96)
        self.assertEqual(retained[-1], b"latest-program-clock")
        self.assertNotEqual(retained[0], b"stale-0")
        snapshot = sink.health_snapshot()
        self.assertTrue(snapshot["writer_backpressured"])
        self.assertIsNotNone(snapshot["writer_backpressure_age_seconds"])
        self.assertEqual(snapshot["pcm_queue_capacity_chunks"], old_chunks)
        self.assertEqual(snapshot["dropped_pcm_chunks"], old_chunks - 95)

    def test_source_retries_do_not_exhaust_after_backoff_sequence(self):
        attempts = []

        def unavailable_source(_cfg):
            attempts.append(time.monotonic())
            raise ConnectionError("origin unavailable")

        sink = IcecastAudioSink(
            "ffmpeg",
            lambda *_args, **_kwargs: None,
            source_factory=unavailable_source,
        )
        stop = _ControlledRetryStop(stop_after=8)
        sink._writer_stop = stop
        cfg = SimpleNamespace(
            icecast_host="stream.example",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_password="",
            stream_codec_profile="",
            stream_bitrate_kbps=0,
        )

        sink._start_connector_worker(cfg)
        sink._connector_thread.join(timeout=1.0)

        self.assertFalse(sink._connector_thread.is_alive())
        self.assertEqual(len(attempts), 8)
        self.assertGreater(len(attempts), 6)
        self.assertEqual(len(stop.wait_calls), len(attempts))
        self.assertGreaterEqual(stop.wait_calls[-1], 30.0)
        self.assertEqual(sink.health_snapshot()["network_error_count"], 8)


if __name__ == "__main__":
    unittest.main()
