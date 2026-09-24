from __future__ import annotations

import unittest
import subprocess
from io import BytesIO
from dataclasses import replace
from unittest.mock import MagicMock, patch

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.station_runtime import StationRuntime


class _FakeSink:
    protocol = "icecast"
    manages_pcm_continuity = True
    instances: list["_FakeSink"] = []

    def __init__(self, *_args, **_kwargs):
        self.cfg = None
        self.running = False
        self.accept = True
        self.chunks: list[bytes] = []
        self.stopped = False
        self.__class__.instances.append(self)

    def ensure_started(self, cfg):
        self.cfg = cfg
        self.running = True
        return self

    def is_running(self):
        return self.running

    def write_pcm(self, chunk):
        if not self.accept:
            return False
        self.chunks.append(bytes(chunk))
        return True

    def stop(self):
        self.running = False
        self.stopped = True

    def health_snapshot(self):
        return {
            "process_running": self.running,
            "mount_healthy": self.running,
            "consecutive_probe_failures": 0,
        }


class _FinishedPcmProducer:
    def __init__(self, payload: bytes):
        self.stdout = BytesIO(payload)

    @staticmethod
    def poll():
        return 0


def _cfg(**changes) -> StationPipelineConfig:
    base = StationPipelineConfig(
        input_uri="test://program",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/lofi",
        icecast_user="source",
        icecast_password="protected-secret",
        local_output_enabled=False,
        output_device_id="",
        stream_codec_profile="aac_128",
        stream_bitrate_kbps=128,
        stream_title="Private track title",
        stream_artist="Private track artist",
        extra_icecast_outputs=(
            {
                "enabled": True,
                "mount": "/lofi-low",
                "stream_codec_profile": "aac_96",
                "stream_bitrate_kbps": 96,
            },
            {
                "enabled": True,
                "mount": "/lofi-flac",
                "stream_codec_profile": "ogg_flac_lossless",
                "stream_bitrate_kbps": 0,
            },
        ),
    )
    return replace(base, **changes)


class MultiQualityRuntimeTests(unittest.TestCase):
    def setUp(self):
        _FakeSink.instances = []
        self.sink_patch = patch(
            "app.audio.station_runtime.IcecastAudioSink", _FakeSink
        )
        self.sink_patch.start()
        self.runtime = StationRuntime(
            process_factory=lambda *_args, **_kwargs: None,
            station_id=2,
        )
        self.runtime.ffmpeg_bin = "ffmpeg.exe"

    def tearDown(self):
        self.runtime.stop()
        self.sink_patch.stop()

    def test_quality_sinks_receive_identical_pcm_and_inherit_credentials(self):
        cfg = _cfg()

        self.assertTrue(self.runtime._ensure_icecast_sink(cfg))
        results = self.runtime._ensure_extra_icecast_sinks(cfg)
        self.runtime._write_pcm_chunk_to_targets(
            b"same-program-pcm", self.runtime._icecast_output_targets()
        )

        self.assertEqual(
            results,
            {
                "icecast:/lofi-low": True,
                "icecast:/lofi-flac": True,
            },
        )
        self.assertEqual(len(_FakeSink.instances), 3)
        self.assertTrue(
            all(sink.chunks == [b"same-program-pcm"] for sink in _FakeSink.instances)
        )
        quality_cfgs = [
            sink.cfg
            for sink in _FakeSink.instances
            if sink.cfg.icecast_mount != "/lofi"
        ]
        self.assertEqual(
            {item.icecast_mount for item in quality_cfgs},
            {"/lofi-low", "/lofi-flac"},
        )
        self.assertEqual(
            {
                item.icecast_mount: item.stream_bitrate_kbps
                for item in quality_cfgs
            },
            {"/lofi-low": 96, "/lofi-flac": 0},
        )
        self.assertTrue(all(item.icecast_user == cfg.icecast_user for item in quality_cfgs))
        self.assertTrue(
            all(item.icecast_password == cfg.icecast_password for item in quality_cfgs)
        )
        self.assertTrue(all(item.stream_title == "" for item in quality_cfgs))
        self.assertTrue(all(item.stream_artist == "" for item in quality_cfgs))
        self.assertEqual(cfg.stream_title, "Private track title")
        self.assertTrue(self.runtime.branch_health()["icecast:/lofi-low"])
        self.assertTrue(self.runtime.branch_health()["icecast:/lofi-flac"])

    def test_one_quality_queue_failure_does_not_stop_other_outputs(self):
        cfg = _cfg()
        self.runtime._ensure_icecast_sink(cfg)
        self.runtime._ensure_extra_icecast_sinks(cfg)
        self.runtime._extra_icecast_sinks["icecast:/lofi-low"].accept = False

        self.runtime._write_pcm_chunk_to_targets(
            b"pcm", self.runtime._icecast_output_targets()
        )

        self.assertEqual(self.runtime._icecast_sink.chunks, [b"pcm"])
        self.assertEqual(
            self.runtime._extra_icecast_sinks["icecast:/lofi-low"].chunks, []
        )
        self.assertEqual(
            self.runtime._extra_icecast_sinks["icecast:/lofi-flac"].chunks,
            [b"pcm"],
        )
        branches = self.runtime.branch_health()
        self.assertTrue(branches["icecast"])
        self.assertFalse(branches["icecast:/lofi-low"])
        self.assertTrue(branches["icecast:/lofi-flac"])

    def test_pcm_pipe_reads_once_and_fans_out_the_same_program_bytes(self):
        cfg = _cfg()
        self.runtime._ensure_icecast_sink(cfg)
        self.runtime._ensure_extra_icecast_sinks(cfg)
        producer = _FinishedPcmProducer(b"authoritative-timeline")

        self.runtime._icecast_pipe_loop(
            producer,
            self.runtime._icecast_sink,
            self.runtime._current_playout_generation(),
        )

        self.assertTrue(
            all(
                sink.chunks == [b"authoritative-timeline"]
                for sink in _FakeSink.instances
            )
        )

    def test_pcm_pipe_phase_locks_after_small_startup_reserve(self):
        class ChunkedStdout:
            def __init__(self):
                self.remaining = 68

            def read(self, _size):
                if self.remaining <= 0:
                    return b""
                self.remaining -= 1
                return b"p" * 4096

            def close(self):
                return None

        class FinishedProducer:
            def __init__(self):
                self.stdout = ChunkedStdout()

            @staticmethod
            def poll():
                return 0

        class ClockedStop:
            def __init__(self, clock):
                self.clock = clock
                self.waits = []
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.waits.append(seconds)
                self.clock[0] += seconds
                return self.stopped

            def set(self):
                self.stopped = True

            def clear(self):
                self.stopped = False

        self.runtime._ensure_icecast_sink(_cfg())
        clock = [100.0]
        original_stop = self.runtime._icecast_pipe_stop
        clocked_stop = ClockedStop(clock)
        self.runtime._icecast_pipe_stop = clocked_stop
        try:
            with patch(
                "app.audio.station_runtime.time.monotonic",
                side_effect=lambda: clock[0],
            ):
                self.runtime._icecast_pipe_loop(
                    FinishedProducer(),
                    self.runtime._icecast_sink,
                    self.runtime._current_playout_generation(),
                )
        finally:
            self.runtime._icecast_pipe_stop = original_stop

        self.assertEqual(len(clocked_stop.waits), 4)
        self.assertAlmostEqual(
            sum(clocked_stop.waits),
            4 * 4096 / (48000 * 2 * 2),
            places=6,
        )
        self.assertTrue(
            all(len(sink.chunks) == 68 for sink in _FakeSink.instances)
        )

    def test_runtime_silence_floor_skips_self_clocked_icecast_queues(self):
        cfg = _cfg()
        self.runtime._ensure_icecast_sink(cfg)
        self.runtime._ensure_extra_icecast_sinks(cfg)

        self.assertEqual(self.runtime._silence_floor_targets(), [])

    def test_quality_settings_are_part_of_runtime_signature(self):
        first = _cfg()
        changed = _cfg(
            extra_icecast_outputs=(
                {
                    "enabled": True,
                    "mount": "/lofi-low",
                    "stream_codec_profile": "aac_96",
                    "stream_bitrate_kbps": 97,
                },
            )
        )

        self.assertNotEqual(
            self.runtime._signature(first), self.runtime._signature(changed)
        )

    def test_duplicate_primary_and_quality_mounts_are_not_fanned_out_twice(self):
        cfg = _cfg(
            extra_icecast_outputs=(
                {"mount": "/lofi", "stream_codec_profile": "aac_96"},
                {"mount": "/lofi-low", "stream_codec_profile": "aac_96"},
                {"mount": "lofi-low", "stream_codec_profile": "aac_320"},
            )
        )

        outputs = self.runtime._extra_output_configs(cfg)

        self.assertEqual(list(outputs), ["icecast:/lofi-low"])
        self.assertEqual(
            outputs["icecast:/lofi-low"].stream_codec_profile, "aac_96"
        )

    def test_string_false_values_do_not_accidentally_enable_outputs(self):
        cfg = _cfg(
            extra_icecast_outputs=(
                {"enabled": "false", "mount": "/lofi-low"},
                {
                    "enabled": "true",
                    "mount": "/lofi-flac",
                    "icecast_public": "false",
                },
            )
        )

        outputs = self.runtime._extra_output_configs(cfg)

        self.assertEqual(list(outputs), ["icecast:/lofi-flac"])
        self.assertFalse(outputs["icecast:/lofi-flac"].icecast_public)

    def test_quality_outputs_hot_refresh_preserves_programme_producer(self):
        cfg = _cfg()
        self.runtime._active_cfg = cfg
        self.runtime._active_signature = self.runtime._signature(cfg)
        producer = MagicMock()
        producer.poll.return_value = None
        self.runtime._process = producer
        self.runtime._ensure_extra_icecast_sinks(cfg)
        retired = list(self.runtime._extra_icecast_sinks.values())

        result = self.runtime.refresh_extra_icecast_outputs(())

        self.assertTrue(result["running"])
        self.assertTrue(result["producer_preserved"])
        self.assertIs(self.runtime._process, producer)
        self.assertEqual(self.runtime._active_cfg.extra_icecast_outputs, ())
        self.assertEqual(self.runtime._extra_icecast_sinks, {})
        self.assertTrue(all(sink.stopped for sink in retired))

    def test_crossfade_continues_on_quality_outputs_when_primary_is_unavailable(self):
        cfg = _cfg()
        self.runtime._active_cfg = cfg
        self.runtime._active_started_monotonic = 1.0
        producer = MagicMock()
        producer.poll.return_value = None

        with (
            patch.object(self.runtime, "_ensure_icecast_sink", return_value=False),
            patch.object(
                self.runtime,
                "_ensure_extra_icecast_sinks",
                return_value={"icecast:/lofi-low": True},
            ),
            patch.object(self.runtime, "_ensure_local_sink", return_value=False),
            patch.object(
                self.runtime,
                "_spawn_crossfade_pcm_producer",
                return_value=producer,
            ) as spawn,
            patch.object(self.runtime, "_start_icecast_pipe_worker") as start_pipe,
            patch.object(self.runtime, "_start_silence_floor_worker"),
        ):
            self.runtime._start_crossfade(cfg)

        self.assertIs(spawn.call_args.args[3], subprocess.PIPE)
        start_pipe.assert_called_once()
        self.assertEqual(self.runtime._backend, "ffmpeg-transition")

    def test_steady_playout_starts_quality_outputs_when_primary_is_unavailable(self):
        cfg = _cfg()
        producer = MagicMock()
        producer.poll.return_value = None

        with (
            patch.object(self.runtime, "_ensure_icecast_sink", return_value=False),
            patch.object(
                self.runtime,
                "_ensure_extra_icecast_sinks",
                return_value={"icecast:/lofi-low": True},
            ),
            patch.object(
                self.runtime,
                "_spawn_icecast_pcm_producer",
                return_value=producer,
            ) as spawn,
            patch.object(self.runtime, "_start_icecast_pipe_worker") as start_pipe,
            patch.object(self.runtime, "_start_silence_floor_worker"),
        ):
            self.runtime._launch_steady_state(cfg, self.runtime._signature(cfg))

        spawn.assert_called_once()
        start_pipe.assert_called_once()
        self.assertEqual(self.runtime._backend, "ffmpeg")
        self.assertFalse(self.runtime.branch_health()["icecast"])


if __name__ == "__main__":
    unittest.main()
