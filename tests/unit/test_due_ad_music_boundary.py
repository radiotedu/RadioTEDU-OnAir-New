from __future__ import annotations

import datetime
import unittest

from app.engine.station_worker import StationWorker


class _QueueRepo:
    def __init__(self, playing, pending):
        self.playing = playing
        self.pending = pending

    def current_playing(self, _station_id):
        return self.playing

    def next_pending(self, _station_id):
        return self.pending


class _RuntimeRegistry:
    def __init__(self, status):
        self._status = status

    def status(self, _station_id):
        return self._status

    def is_process_running(self, _station_id):
        return True


class _AdRepo:
    def __init__(self, due):
        self.due = due

    def next_due(self, _station_id):
        return self.due


class DueAdMusicBoundaryTests(unittest.TestCase):
    def _worker(self, due_ad):
        worker = StationWorker.__new__(StationWorker)
        worker.station_id = 4
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        started_at = (now - datetime.timedelta(seconds=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        worker.queue_repo = _QueueRepo(
            {
                "id": 12,
                "track_id": 501,
                "started_at": started_at,
                "duration": 10.0,
                "track_type": "music",
            },
            {"track_type": "music"},
        )
        worker.runtime_registry = _RuntimeRegistry(
            {
                "running": True,
                "program_running": True,
                "producer_eof": False,
                "active_input_uri": "test://current-song",
            }
        )
        worker.ad_repo = _AdRepo(due_ad)
        worker._ads_enabled = lambda: True
        worker._default_crossfade_seconds = lambda: 3.0
        worker._cached_track_duration = (
            lambda *_args, **_kwargs: 10.0
        )
        worker._track_runtime_fields = lambda _track_id: (
            "test://current-song",
            "Current Song",
            "Artist",
            "",
            "music",
        )
        worker.completed = []
        worker._complete_queue_item = lambda item: worker.completed.append(item["id"])
        return worker

    def test_due_campaign_ad_keeps_current_song_until_its_real_end(self):
        worker = self._worker({"id": 23})

        advanced = worker._advance_playing_queue_item()

        self.assertFalse(advanced)
        self.assertEqual(worker.completed, [])

    def test_music_to_music_crossfade_remains_enabled_without_due_ad(self):
        worker = self._worker(None)

        advanced = worker._advance_playing_queue_item()

        self.assertTrue(advanced)
        self.assertEqual(worker.completed, [12])

    def test_underreported_catalog_duration_does_not_end_song_early(self):
        worker = self._worker(None)
        worker._cached_track_duration = (
            lambda *_args, **_kwargs: 20.0
        )

        advanced = worker._advance_playing_queue_item()

        self.assertFalse(advanced)
        self.assertEqual(worker.completed, [])

    def test_crossfade_uses_decoder_elapsed_not_earlier_queue_clock(self):
        worker = self._worker(None)
        worker.runtime_registry._status["elapsed"] = 5.5

        advanced = worker._advance_playing_queue_item()

        self.assertFalse(advanced)
        self.assertEqual(worker.completed, [])

    def test_dead_source_at_due_ad_boundary_retries_song_instead_of_skipping_tail(self):
        worker = self._worker({"id": 23})
        started_at = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) - datetime.timedelta(seconds=11)
        worker.queue_repo.playing["started_at"] = started_at.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        worker.runtime_registry._status = {
            "running": False,
            "program_running": False,
            "producer_eof": False,
            "active_input_uri": "test://current-song",
        }
        retries = []
        worker._restart_playing_queue_item_if_runtime_mismatched = (
            lambda item, *, start_offset_seconds: retries.append(
                (item["id"], start_offset_seconds)
            )
            or True
        )

        advanced = worker._advance_playing_queue_item()

        self.assertFalse(advanced)
        self.assertEqual(worker.completed, [])
        self.assertEqual(retries, [(12, 0.0)])


if __name__ == "__main__":
    unittest.main()
