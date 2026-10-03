from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_audio_sink import IcecastAudioSink
from app.audio.output_health import icecast_mount_transport_is_healthy
import app.audio.station_runtime as runtime_module
from app.audio.station_runtime import StationRuntime
from app.engine.runtime_registry import StationRuntimeRegistry


def flowing_full_fifo():
    return {
        "process_running": True, "writer_running": True,
        "network_writer_running": True, "mount_healthy": True,
        "source_response_confirmed": True, "encoded_bytes_sent": 10000,
        "last_write_age_seconds": 0.1, "last_network_write_age_seconds": 0.1,
        "writer_backpressured": True, "writer_backpressure_age_seconds": 40,
        "queued_pcm_seconds": 21.845, "queued_pcm_chunks": 1024,
        "pcm_queue_capacity_chunks": 1024,
    }


def test_confirmed_flowing_full_fifo_does_not_disconnect_its_source():
    health = flowing_full_fifo()
    assert StationRuntimeRegistry._output_failure_confirmed(health) is False
    assert icecast_mount_transport_is_healthy(health) is True
    assert health["writer_backpressured"] is True


@pytest.mark.parametrize("field,value", [
    ("source_response_confirmed", False), ("encoded_bytes_sent", 0),
    ("last_write_age_seconds", 11), ("last_network_write_age_seconds", 11),
    ("process_running", False), ("writer_running", False),
    ("network_writer_running", False), ("writer_failed", True),
    ("network_failed", True), ("mount_healthy", False),
    ("delivery_loss_unrecovered", True),
])
def test_full_fifo_without_confirmed_progress_still_recovers(field, value):
    health = flowing_full_fifo()
    health[field] = value
    assert StationRuntimeRegistry._output_failure_confirmed(health) is True
    assert icecast_mount_transport_is_healthy(health) is False


def starting_source():
    return {
        "process_running": False, "writer_running": True,
        "network_writer_running": True, "connector_running": True,
        "connection_starting": True, "connection_startup_age_seconds": 0.5,
        "connection_startup_timeout_seconds": 11, "encoded_bytes_sent": 0,
        "source_response_confirmed": False, "mount_healthy": None,
    }


def test_bounded_first_connection_wait_is_not_a_confirmed_failure():
    health = starting_source()
    assert StationRuntimeRegistry._output_failure_confirmed(health) is False
    assert icecast_mount_transport_is_healthy(health) is False


@pytest.mark.parametrize("field,value", [
    ("connection_starting", False), ("connector_running", False),
    ("connection_startup_age_seconds", 11), ("connection_startup_age_seconds", None),
    ("connection_startup_timeout_seconds", 99999), ("writer_running", False),
    ("network_failed", True), ("writer_failed", True), ("mount_healthy", False),
])
def test_expired_or_failed_first_connection_does_not_hide_a_dead_output(field, value):
    health = starting_source()
    health[field] = value
    assert StationRuntimeRegistry._output_failure_confirmed(health) is True


def test_unconfirmed_source_writes_are_not_reported_healthy():
    health = flowing_full_fifo()
    health.update(writer_backpressured=False, source_response_confirmed=False)
    assert icecast_mount_transport_is_healthy(health) is False


def test_sink_exposes_only_a_bounded_live_first_connection_wait():
    sink = IcecastAudioSink("ffmpeg", lambda *_args, **_kwargs: None)
    sink._connector_thread = SimpleNamespace(is_alive=lambda: True)
    sink._writer_thread = SimpleNamespace(is_alive=lambda: True)
    sink._connector_started_monotonic = time.monotonic()
    sink._connector_initial_delay_seconds = 1.0
    status = sink.health_snapshot()
    assert status["connection_starting"] is True
    assert status["connection_startup_timeout_seconds"] == 11
    sink._encoded_bytes_sent = 1
    assert sink.health_snapshot()["connection_starting"] is False
    sink._encoded_bytes_sent = 0
    sink._connector_started_monotonic = time.monotonic() - 12
    assert sink.health_snapshot()["connection_starting"] is False


def test_all_quality_connections_start_with_at_most_one_second_spread(monkeypatch):
    spreads = []

    class Sink:
        def __init__(self, *_args, **kwargs):
            spreads.append(kwargs["initial_connect_spread_sec"])
            self.protocol = "icecast"

        def ensure_started(self, *_args, **_kwargs):
            return None

        def is_running(self):
            return True

        def stop(self, **_kwargs):
            pass

    monkeypatch.setattr(runtime_module, "IcecastAudioSink", Sink)
    cfg = StationPipelineConfig(
        input_uri="E:/music/song.mp3", icecast_host="127.0.0.1", icecast_port=8000,
        icecast_mount="/radio", icecast_user="source", icecast_password="test",
        local_output_enabled=False, output_device_id="",
    )
    cfg = replace(cfg, extra_icecast_outputs=({"enabled": True, "icecast_mount": "/radio-low"},))
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    assert runtime._ensure_icecast_sink(cfg) is True
    runtime._ensure_extra_icecast_sinks(cfg)
    assert len(spreads) == 2
    assert all(0 <= value <= 1 for value in spreads)
