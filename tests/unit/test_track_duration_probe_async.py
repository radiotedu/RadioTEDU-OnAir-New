from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app.engine.station_worker as station_worker_module
from app.engine.station_worker import StationWorker


class AsyncTrackDurationProbeTests(unittest.TestCase):
    def test_transition_lookup_never_waits_for_network_path_or_ffprobe(self):
        class ProbeDatabase:
            def __init__(self):
                self.updates = []

            def execute(self, _query, params):
                self.updates.append(tuple(params))

            def commit(self):
                return None

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "track.mp3"
            media_path.write_bytes(b"test media")
            worker = StationWorker.__new__(StationWorker)
            probe_db = ProbeDatabase()
            network_path = r"\\radio-media\library\track.mp3"
            resolution_started = threading.Event()
            resolution_finished = threading.Event()
            probe_started = threading.Event()
            probe_finished = threading.Event()

            def slow_resolve(_path):
                resolution_started.set()
                time.sleep(0.2)
                resolution_finished.set()
                return str(media_path)

            def slow_probe(_path, *, timeout_seconds):
                self.assertEqual(timeout_seconds, 5.0)
                probe_started.set()
                time.sleep(0.2)
                probe_finished.set()
                return 12.5

            with patch(
                "app.engine.station_worker.resolve_runtime_media_path",
                side_effect=slow_resolve,
            ), patch(
                "app.audio.audio_processing.probe_duration",
                side_effect=slow_probe,
            ), patch(
                "app.engine.station_worker.get_connection",
                return_value=probe_db,
            ):
                started = time.monotonic()
                first_result = worker._cached_track_duration(1, network_path)
                lookup_elapsed = time.monotonic() - started

                self.assertEqual(first_result, 0.0)
                self.assertLess(lookup_elapsed, 0.15)
                self.assertTrue(resolution_started.wait(1.0))
                self.assertTrue(probe_started.wait(1.0))
                self.assertTrue(resolution_finished.wait(1.0))
                self.assertTrue(probe_finished.wait(1.0))

                deadline = time.monotonic() + 1.0
                duration = 0.0
                while time.monotonic() < deadline and duration <= 0.0:
                    duration = worker._cached_track_duration(1, network_path)
                    if duration <= 0.0:
                        time.sleep(0.01)

            self.assertEqual(duration, 12.5)
            self.assertEqual(probe_db.updates, [(12.5, 1, network_path)])

    def test_replaced_file_invalidates_duration_and_only_stable_probe_is_persisted(self):
        class ProbeDatabase:
            def __init__(self):
                self.updates = []

            def execute(self, _query, params):
                self.updates.append(tuple(params))

            def commit(self):
                return None

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "track.mp3"
            media_path.write_bytes(b"first media version")
            probe_db = ProbeDatabase()
            durations = iter((11.0, 17.0))
            track_id = time.time_ns()
            network_path = r"\\radio-media\library\track.mp3"

            def wait_for_duration(expected):
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    key = station_worker_module._duration_probe_request_key(
                        track_id, network_path
                    )
                    with station_worker_module._DURATION_PROBE_LOCK:
                        cached = station_worker_module._DURATION_PROBE_CACHE.get(key)
                        pending = key in station_worker_module._DURATION_PROBE_PENDING
                    if cached and cached[0] == expected and not pending:
                        return cached
                    time.sleep(0.01)
                self.fail(f"duration probe did not publish {expected}")

            with patch(
                "app.engine.station_worker.resolve_runtime_media_path",
                return_value=str(media_path),
            ), patch(
                "app.audio.audio_processing.probe_duration",
                side_effect=lambda *_args, **_kwargs: next(durations),
            ), patch(
                "app.engine.station_worker.get_connection",
                return_value=probe_db,
            ), patch(
                "app.engine.station_worker._DURATION_PROBE_IDENTITY_TTL_SECONDS",
                0.0,
            ):
                station_worker_module._schedule_track_duration_probe(
                    track_id, network_path
                )
                first = wait_for_duration(11.0)
                self.assertIsNotNone(first[2])

                media_path.write_bytes(b"a replacement media file with new size")
                station_worker_module._schedule_track_duration_probe(
                    track_id, network_path
                )
                second = wait_for_duration(17.0)

            self.assertNotEqual(first[2], second[2])
            self.assertEqual(
                probe_db.updates,
                [(11.0, track_id, network_path), (17.0, track_id, network_path)],
            )

    def test_file_changed_during_probe_is_not_cached_or_persisted(self):
        class ProbeDatabase:
            def __init__(self):
                self.updates = []

            def execute(self, _query, params):
                self.updates.append(tuple(params))

            def commit(self):
                return None

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "track.mp3"
            media_path.write_bytes(b"before probe")
            probe_db = ProbeDatabase()
            probe_started = threading.Event()
            allow_probe_to_finish = threading.Event()
            probe_calls = 0
            track_id = time.time_ns()
            network_path = r"\\radio-media\library\track.mp3"

            def probe_duration(_path, *, timeout_seconds):
                nonlocal probe_calls
                self.assertEqual(timeout_seconds, 5.0)
                probe_calls += 1
                if probe_calls == 1:
                    return 11.0
                probe_started.set()
                self.assertTrue(allow_probe_to_finish.wait(1.0))
                return 19.0

            with patch(
                "app.engine.station_worker.resolve_runtime_media_path",
                return_value=str(media_path),
            ), patch(
                "app.audio.audio_processing.probe_duration",
                side_effect=probe_duration,
            ), patch(
                "app.engine.station_worker.get_connection",
                return_value=probe_db,
            ), patch(
                "app.engine.station_worker._DURATION_PROBE_IDENTITY_TTL_SECONDS",
                0.0,
            ):
                station_worker_module._schedule_track_duration_probe(
                    track_id, network_path
                )
                deadline = time.monotonic() + 2.0
                cached_first = None
                while time.monotonic() < deadline:
                    key = station_worker_module._duration_probe_request_key(
                        track_id, network_path
                    )
                    with station_worker_module._DURATION_PROBE_LOCK:
                        cached_first = station_worker_module._DURATION_PROBE_CACHE.get(key)
                        pending = key in station_worker_module._DURATION_PROBE_PENDING
                    if cached_first and cached_first[0] == 11.0 and not pending:
                        break
                    time.sleep(0.01)
                self.assertTrue(cached_first and cached_first[0] == 11.0)
                media_path.write_bytes(b"changed before replacement probe")
                station_worker_module._schedule_track_duration_probe(
                    track_id, network_path
                )
                self.assertTrue(probe_started.wait(1.0))
                media_path.write_bytes(b"replacement after ffprobe started")
                allow_probe_to_finish.set()

                deadline = time.monotonic() + 2.0
                cached = "probe still pending"
                while time.monotonic() < deadline:
                    key = station_worker_module._duration_probe_request_key(
                        track_id, network_path
                    )
                    with station_worker_module._DURATION_PROBE_LOCK:
                        pending = key in station_worker_module._DURATION_PROBE_PENDING
                        cached = station_worker_module._DURATION_PROBE_CACHE.get(key)
                    if not pending:
                        break
                    time.sleep(0.01)

            self.assertIsNone(cached)
            self.assertEqual(probe_db.updates, [(11.0, track_id, network_path)])


if __name__ == "__main__":
    unittest.main()
