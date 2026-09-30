import queue
import time

import pytest

from app.audio.ffmpeg_pipeline import build_ffmpeg_encoded_sink_cmd
from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.output_health import icecast_mount_transport_is_healthy
from app.audio.shoutcast_audio_sink import (
    ShoutcastAudioSink,
    ShoutcastProtocolError,
    perform_shoutcast_v1_handshake,
)


def _config(**overrides):
    values = {
        "input_uri": "silence://continuity",
        "icecast_host": "127.0.0.1",
        "icecast_port": 8001,
        "icecast_mount": "/stream/1",
        "icecast_user": "source",
        "icecast_password": "test-source-password",
        "local_output_enabled": False,
        "output_device_id": "",
        "icecast_enabled": True,
        "stream_codec_profile": "mp3_128",
        "stream_bitrate_kbps": 128,
        "station_name": "RadioTEDU Test",
        "icecast_stream_name": "RadioTEDU Test",
        "icecast_genre": "Test",
        "icecast_url": "https://example.invalid/radio",
        "source_protocol": "shoutcast",
    }
    values.update(overrides)
    return StationPipelineConfig(**values)


class FakeSocket:
    def __init__(self, responses=(b"OK2\r\nicy-caps:11\r\n\r\n",), fail_send_at=None):
        self.responses = list(responses)
        self.fail_send_at = fail_send_at
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, payload):
        if self.fail_send_at is not None and len(self.sent) >= self.fail_send_at:
            raise TimeoutError("simulated half-open socket")
        self.sent.append(bytes(payload))

    def recv(self, _size):
        return self.responses.pop(0) if self.responses else b""

    def shutdown(self, _how):
        self.closed = True

    def close(self):
        self.closed = True


class FakePipe:
    def __init__(self, reads=()):
        self.reads = list(reads)
        self.writes = []
        self.closed = False

    def write(self, payload):
        self.writes.append(bytes(payload))

    def flush(self):
        return None

    def read(self, _size):
        return self.reads.pop(0) if self.reads else b""

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, encoded_reads=(b"encoded-audio",)):
        self.stdin = FakePipe()
        self.stdout = FakePipe(encoded_reads)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_legacy_handshake_accepts_ok2_and_sends_sanitized_icy_headers():
    source_socket = FakeSocket()
    cfg = _config(icecast_stream_name="RadioTEDU\r\nInjected: no")

    perform_shoutcast_v1_handshake(source_socket, cfg, timeout_seconds=0.5)

    assert source_socket.sent[0] == b"test-source-password\r\n"
    headers = source_socket.sent[1]
    assert b"icy-name:RadioTEDU  Injected: no\r\n" in headers
    assert b"icy-br:128\r\n" in headers
    assert b"content-type:audio/mpeg\r\n" in headers
    assert b"test-source-password" not in headers


@pytest.mark.parametrize(
    "responses",
    [
        (b"invalid password\r\n",),
        (b"",),
    ],
)
def test_legacy_handshake_rejects_wrong_password_and_empty_response(responses):
    source_socket = FakeSocket(responses=responses)

    with pytest.raises(ShoutcastProtocolError) as error:
        perform_shoutcast_v1_handshake(source_socket, _config())

    assert "test-source-password" not in str(error.value)


def test_legacy_handshake_rejects_unsupported_codec_before_streaming():
    source_socket = FakeSocket()

    with pytest.raises(ShoutcastProtocolError, match="MP3 or AAC"):
        perform_shoutcast_v1_handshake(
            source_socket,
            _config(stream_codec_profile="opus_196", stream_bitrate_kbps=196),
        )


def test_encoded_sink_command_contains_no_destination_or_credential():
    cfg = _config()

    command = build_ffmpeg_encoded_sink_cmd(cfg, "ffmpeg.exe")
    rendered = " ".join(command)

    assert command[-1] == "pipe:1"
    assert "test-source-password" not in rendered
    assert "127.0.0.1" not in rendered
    assert "8001" not in rendered


def test_preserved_stop_and_restart_keep_queued_and_inflight_pcm(monkeypatch):
    source_socket = FakeSocket()
    process = FakeProcess(encoded_reads=(b"",))
    sink = ShoutcastAudioSink(
        "ffmpeg.exe",
        lambda *args, **kwargs: process,
        socket_factory=lambda *args, **kwargs: source_socket,
    )
    sink._pcm_queue.put_nowait(b"queued-programme-pcm")
    sink._writer_pending_chunk = b"inflight-programme-pcm"
    started_with_preserved_pcm = []
    monkeypatch.setattr(
        sink,
        "_start_threads",
        lambda *, preserve_pcm=False: started_with_preserved_pcm.append(
            preserve_pcm
        ),
    )

    try:
        sink.stop(preserve_pcm=True)
        assert sink._pcm_queue.get_nowait() == b"queued-programme-pcm"
        sink._pcm_queue.put_nowait(b"queued-programme-pcm")

        sink.ensure_started(_config(), preserve_pcm=True)

        assert started_with_preserved_pcm == [True]
        assert sink._writer_pending_chunk == b"inflight-programme-pcm"
        assert sink._pcm_queue.get_nowait() == b"queued-programme-pcm"
    finally:
        sink.stop()

    assert sink._pcm_queue.empty()
    assert sink._writer_pending_chunk is None


def test_queue_overflow_stays_unhealthy_until_a_recovery_connection_delivers():
    sink = ShoutcastAudioSink("ffmpeg.exe", lambda *args, **kwargs: None)
    sink._pcm_queue = queue.Queue(maxsize=1)
    sink._pcm_queue.put_nowait(b"accepted-before-overflow")
    sink._process = FakeProcess()
    sink._socket = FakeSocket()
    sink._handshake_accepted = True
    sink._encoded_bytes_sent = 1

    assert sink.write_pcm(b"dropped-on-overflow") is False
    health = sink.health_snapshot()
    assert health["dropped_pcm_chunks"] == 1
    assert health["delivery_loss_unrecovered"] is True
    assert health["mount_healthy"] is False
    assert not icecast_mount_transport_is_healthy(health)

    # Bytes from the connection on which loss happened are not recovery proof.
    sink._record_network_delivery(10, sink._connection_epoch)
    assert sink.health_snapshot()["delivery_loss_unrecovered"] is True

    # A replacement connection must send encoded audio before health clears.
    sink._connection_epoch += 1
    sink._encoded_bytes_sent = 0
    sink._record_network_delivery(10, sink._connection_epoch)
    recovered = sink.health_snapshot()
    assert recovered["delivery_loss_unrecovered"] is False
    assert recovered["dropped_pcm_chunks"] == 1
    assert recovered["delivery_loss_count"] == 1
    assert recovered["no_drop_since_start"] is False
    assert recovered["mount_healthy"] is True
    assert recovered["remote_mount_verified"] is False
    assert recovered["remote_mount_verification_supported"] is False
    assert recovered["public_listener_verified"] is False


def test_stale_connection_failure_cannot_kill_replacement_encoder():
    sink = ShoutcastAudioSink("ffmpeg.exe", lambda *_args, **_kwargs: None)
    old_process = FakeProcess()
    replacement_process = FakeProcess()
    sink._process = replacement_process
    sink._connection_epoch = 2
    sink._network_failed = False

    sink._fail_network(process=old_process, connection_epoch=1)

    assert replacement_process.poll() is None
    assert sink._network_failed is False


def test_stale_writer_cannot_replace_pending_pcm_for_new_connection():
    sink = ShoutcastAudioSink("ffmpeg.exe", lambda *_args, **_kwargs: None)
    old_process = FakeProcess()
    replacement_process = FakeProcess()
    sink._process = replacement_process
    sink._connection_epoch = 2
    sink._writer_pending_chunk = b"replacement-pcm"

    stored = sink._store_writer_pending_chunk(
        b"stale-old-session-pcm",
        process=old_process,
        connection_epoch=1,
        allow_stopping=True,
    )

    assert stored is False
    assert sink._writer_pending_chunk == b"replacement-pcm"


def test_writer_preserves_dequeued_pcm_when_stop_clears_process_before_store():
    sink = ShoutcastAudioSink("ffmpeg.exe", lambda *_args, **_kwargs: None)
    old_process = FakeProcess()
    sink._process = None
    sink._connection_epoch = 1

    stored = sink._store_writer_pending_chunk(
        b"dequeued-before-stop",
        process=old_process,
        connection_epoch=1,
        allow_stopping=True,
    )

    assert stored is True
    assert sink._writer_pending_chunk == b"dequeued-before-stop"


def test_late_old_connection_write_does_not_refresh_replacement_health():
    sink = ShoutcastAudioSink("ffmpeg.exe", lambda *_args, **_kwargs: None)
    sink._connection_epoch = 2

    sink._record_network_delivery(512, 1)

    assert sink._encoded_bytes_sent == 0
    assert sink._last_network_write_monotonic is None


def test_shoutcast_health_exposes_bounded_first_audio_startup_age():
    sink = ShoutcastAudioSink("ffmpeg.exe", lambda *args, **kwargs: None)
    sink._process = FakeProcess()
    sink._handshake_accepted = True
    sink._connection_started_monotonic = time.monotonic() - 31.0

    health = sink.health_snapshot()

    assert health["source_protocol"] == "shoutcast"
    assert health["connection_age_seconds"] >= 30.0
    assert health["first_audio_startup_timeout_seconds"] == 30.0
    assert health["encoded_bytes_sent"] == 0


def test_shared_transport_health_rejects_unrecovered_pcm_loss():
    healthy_transport = {
        "mount_healthy": True,
        "process_running": True,
        "writer_running": True,
        "network_writer_running": True,
        "writer_failed": False,
        "network_failed": False,
        "last_write_age_seconds": 0.0,
        "last_network_write_age_seconds": 0.0,
        "delivery_loss_unrecovered": True,
    }

    assert not icecast_mount_transport_is_healthy(healthy_transport)

    healthy_transport["delivery_loss_unrecovered"] = False
    assert icecast_mount_transport_is_healthy(healthy_transport)


def test_half_open_source_marks_network_failed_and_stops_encoder():
    source_socket = FakeSocket(fail_send_at=2)
    process = FakeProcess(encoded_reads=(b"encoded-audio",))
    sink = ShoutcastAudioSink(
        "ffmpeg.exe",
        lambda *args, **kwargs: process,
        socket_factory=lambda *args, **kwargs: source_socket,
        connect_timeout_sec=0.1,
        handshake_timeout_sec=0.1,
        write_timeout_sec=0.1,
    )
    try:
        sink.ensure_started(_config())
        deadline = time.monotonic() + 1.0
        while not sink.health_snapshot()["network_failed"] and time.monotonic() < deadline:
            time.sleep(0.01)

        health = sink.health_snapshot()
        assert health["network_failed"] is True
        assert health["connection_healthy"] is False
        assert process.poll() is not None
    finally:
        sink.stop()


def test_empty_encoder_payload_is_a_transport_failure():
    source_socket = FakeSocket()
    process = FakeProcess(encoded_reads=(b"",))
    sink = ShoutcastAudioSink(
        "ffmpeg.exe",
        lambda *args, **kwargs: process,
        socket_factory=lambda *args, **kwargs: source_socket,
    )
    try:
        sink.ensure_started(_config())
        deadline = time.monotonic() + 1.0
        while not sink.health_snapshot()["network_failed"] and time.monotonic() < deadline:
            time.sleep(0.01)

        assert sink.health_snapshot()["network_failed"] is True
        assert sink.health_snapshot()["encoded_bytes_sent"] == 0
    finally:
        sink.stop()
