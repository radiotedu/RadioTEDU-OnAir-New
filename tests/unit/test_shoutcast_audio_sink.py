import time

import pytest

from app.audio.ffmpeg_pipeline import build_ffmpeg_encoded_sink_cmd
from app.audio.gst_pipeline import StationPipelineConfig
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
