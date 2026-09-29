from __future__ import annotations

import datetime
import unittest

from app.engine.station_worker import StationWorker
from app.engine import station_worker as station_worker_module


class _QueueRepo:
    def __init__(self, playing, pending):
        self.playing = playing
        self.pending = pending
        self.failed = []
        self.deferred = []

    def current_playing(self, _station_id):
        return self.playing

    def next_pending(self, _station_id):
        return self.pending

    def mark_failed(self, item_id):
        self.failed.append(int(item_id))

    def defer(self, item_id, *, retry_after_seconds, error=""):
        self.deferred.append((int(item_id), retry_after_seconds, error))
        if self.playing and int(self.playing.get("id", 0)) == int(item_id):
            self.playing = None
        return 1


class _PlayoutState:
    def __init__(self):
        self.values = []

    def set_current(self, station_id, source, item_id, **_kwargs):
        self.values.append((int(station_id), str(source), item_id))


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
        self.playing = due
        self.done = []
        self.failed = []
        self.deferred = []

    def next_due(self, _station_id):
        return self.due

    def current_playing(self, _station_id):
        return self.playing

    def mark_playing(self, item_id):
        return None

    def mark_done(self, item_id):
        self.done.append(int(item_id))

    def mark_failed(self, item_id):
        self.failed.append(int(item_id))

    def defer(self, item_id, *, retry_after_seconds, error=""):
        self.deferred.append((int(item_id), retry_after_seconds, error))
        if self.playing and int(self.playing.get("id", 0)) == int(item_id):
            self.playing = None
            self.due = None
        return 1


class DueAdMusicBoundaryTests(unittest.TestCase):
    def _worker(self, due_ad):
        station_worker_module._RESTART_SUPPRESSION.clear()
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
        worker.playout_state = _PlayoutState()
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
        worker._broadcast_worker_state = lambda **_kwargs: None
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

    def test_max_timeout_with_live_mismatched_source_retries_song(self):
        worker = self._worker({"id": 23})
        started_at = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) - datetime.timedelta(seconds=2400)
        worker.queue_repo.playing["started_at"] = started_at.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        worker.runtime_registry._status = {
            "running": True,
            "program_running": True,
            "producer_eof": False,
            "active_input_uri": "test://unexpected-source",
        }
        retries = []
        worker._restart_playing_queue_item_if_runtime_mismatched = (
            lambda item, *, start_offset_seconds: retries.append(
                (item["id"], start_offset_seconds)
            )
            or False
        )

        advanced = worker._advance_playing_queue_item()

        self.assertFalse(advanced)
        self.assertEqual(worker.completed, [])
        self.assertEqual(retries, [(12, 0.0)])

    def test_restart_limit_defers_queue_item_for_later_retry(self):
        worker = self._worker({"id": 23})
        playing = worker.queue_repo.playing
        worker._track_runtime_fields = lambda _track_id: (
            "test://unfinished-song",
            "Unfinished Song",
            "Artist",
            "",
            "music",
        )
        station_worker_module._RESTART_SUPPRESSION[(4, "manual", 12)] = {
            "attempts": station_worker_module._MAX_RESTART_ATTEMPTS_PER_ITEM,
            "next_allowed": 0.0,
            "reason": "",
        }

        recovered = worker._restart_playing_queue_item_if_runtime_mismatched(playing)

        self.assertFalse(recovered)
        self.assertIsNone(worker.queue_repo.playing)
        self.assertEqual(worker.queue_repo.failed, [])
        self.assertEqual(len(worker.queue_repo.deferred), 1)
        deferred_id, retry_after, error = worker.queue_repo.deferred[0]
        self.assertEqual(deferred_id, 12)
        self.assertGreater(retry_after, 0)
        self.assertIn("runtime_mismatch", error)
        self.assertNotIn((4, "manual", 12), station_worker_module._RESTART_SUPPRESSION)

    def test_dead_matching_runtime_is_restarted(self):
        worker = self._worker({"id": 23})
        worker.runtime_registry._status = {
            "running": False,
            "program_running": False,
            "producer_eof": False,
            "active_input_uri": "test://unfinished-song",
        }
        worker._track_runtime_fields = lambda _track_id: (
            "test://unfinished-song",
            "Unfinished Song",
            "Artist",
            "",
            "music",
        )
        starts = []
        worker._start_runtime_station = (
            lambda *_args, **_kwargs: starts.append("restarted")
        )

        recovered = worker._restart_playing_queue_item_if_runtime_mismatched(
            worker.queue_repo.playing
        )

        self.assertTrue(recovered)
        self.assertEqual(starts, ["restarted"])
        self.assertEqual(worker.queue_repo.failed, [])

    def test_transient_restart_failure_keeps_current_queue_item_playing(self):
        worker = self._worker({"id": 23})
        playing = worker.queue_repo.playing
        worker._track_runtime_fields = lambda _track_id: (
            "test://unfinished-song",
            "Unfinished Song",
            "Artist",
            "",
            "music",
        )

        def fail_transiently(*_args, **_kwargs):
            raise RuntimeError("temporary source restart failure")

        worker._start_runtime_station = fail_transiently

        recovered = worker._restart_playing_queue_item_if_runtime_mismatched(playing)

        self.assertFalse(recovered)
        self.assertIs(worker.queue_repo.playing, playing)
        self.assertEqual(worker.queue_repo.failed, [])
        self.assertEqual(worker.playout_state.values[-1], (4, "manual", 12))

    def test_failed_ad_runtime_recovery_retains_ad_ownership(self):
        worker = self._worker({
            "id": 23,
            "track_id": 711,
            "started_at": datetime.datetime.now(datetime.timezone.utc)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%d %H:%M:%S"),
            "duration": 20.0,
        })
        worker.runtime_registry._status["active_input_uri"] = "test://other-source"
        worker._track_runtime_fields = lambda _track_id: (
            "test://powerapp-ad",
            "PowerApp",
            "RadioTEDU",
            "",
            "ad",
        )
        worker._start_runtime_station = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("transient output failure")
        )

        recovered = worker._restart_playing_ad_item_if_runtime_mismatched(
            worker.ad_repo.playing
        )

        self.assertFalse(recovered)
        self.assertEqual(worker.ad_repo.failed, [])
        self.assertEqual(worker.playout_state.values[-1], (4, "ads", 23))

    def test_transient_ad_start_failure_persists_retry_and_releases_playout(self):
        worker = self._worker(
            {
                "id": 23,
                "track_id": 711,
                "started_at": "",
                "duration": 20.0,
            }
        )
        worker._track_runtime_fields = lambda _track_id: (
            "test://powerapp-ad",
            "PowerApp",
            "RadioTEDU",
            "",
            "ad",
        )
        worker._is_transient_output_failure = lambda _error: True

        def fail_transiently(*_args, **_kwargs):
            raise RuntimeError("temporary output connector failure")

        worker._start_runtime_station = fail_transiently

        result = worker._play_managed_item(
            source="ads",
            item_id=23,
            track_id=711,
            mark_playing=worker.ad_repo.mark_playing,
            mark_done=worker.ad_repo.mark_done,
            mark_failed=worker.ad_repo.mark_failed,
            auto_done=False,
        )

        self.assertEqual(result["reason"], "output_retry_deferred")
        self.assertIsNone(worker.ad_repo.playing)
        self.assertEqual(worker.ad_repo.done, [])
        self.assertEqual(worker.ad_repo.failed, [])
        self.assertEqual(worker.ad_repo.deferred[0][0], 23)
        self.assertEqual(worker.playout_state.values[-1], (4, "none", None))

    def test_elapsed_time_without_runtime_does_not_consume_ad(self):
        worker = self._worker(None)
        started_at = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) - datetime.timedelta(seconds=45)
        worker.ad_repo.playing = {
            "id": 23,
            "track_id": 711,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": 20.0,
        }
        worker.runtime_registry = None
        worker._track_runtime_fields = lambda _track_id: (
            "test://powerapp-ad",
            "PowerApp",
            "RadioTEDU",
            "",
            "ad",
        )

        completed = worker._advance_playing_ad_item()

        self.assertFalse(completed)
        self.assertEqual(worker.ad_repo.done, [])
        self.assertEqual(worker.ad_repo.failed, [])

    def test_short_jingle_uri_mismatch_retries_without_clean_eof(self):
        worker = self._worker(None)
        started_at = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) - datetime.timedelta(seconds=0.5)
        worker.queue_repo.playing.update(
            started_at=started_at.strftime("%Y-%m-%d %H:%M:%S"),
            duration=1.0,
            track_type="jingle",
        )
        worker.runtime_registry._status.update(
            running=True,
            program_running=True,
            producer_eof=False,
            active_input_uri="test://other-source",
        )
        worker._track_runtime_fields = lambda _track_id: (
            "test://sweeper",
            "TEDU Sweeper",
            "RadioTEDU",
            "",
            "jingle",
        )
        retries = []
        worker._restart_playing_queue_item_if_runtime_mismatched = (
            lambda item, *, start_offset_seconds: retries.append(
                (item["id"], start_offset_seconds)
            )
            or False
        )

        advanced = worker._advance_playing_queue_item()

        self.assertFalse(advanced)
        self.assertEqual(worker.completed, [])
        self.assertEqual(retries, [(12, 0.0)])

    def test_short_jingle_completes_only_after_clean_eof(self):
        worker = self._worker(None)
        started_at = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) - datetime.timedelta(seconds=2)
        worker.queue_repo.playing.update(
            started_at=started_at.strftime("%Y-%m-%d %H:%M:%S"),
            duration=1.0,
            track_type="jingle",
        )
        worker.runtime_registry._status.update(
            running=True,
            program_running=False,
            producer_eof=True,
            active_input_uri="test://current-song",
        )
        worker._track_runtime_fields = lambda _track_id: (
            "test://current-song",
            "TEDU Sweeper",
            "RadioTEDU",
            "",
            "jingle",
        )

        advanced = worker._advance_playing_queue_item()

        self.assertTrue(advanced)
        self.assertEqual(worker.completed, [12])


if __name__ == "__main__":
    unittest.main()
