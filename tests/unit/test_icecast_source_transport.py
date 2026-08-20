from __future__ import annotations

import base64

import pytest

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_source_transport import (
    IcecastSourceProtocolError,
    IcecastSourceTransport,
)


class FakeSocket:
    def __init__(self, response=b"HTTP/1.1 200 OK\r\n\r\n"):
        self.response = response
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, payload):
        self.sent.append(bytes(payload))

    def recv(self, _size):
        response, self.response = self.response, b""
        return response

    def shutdown(self, _how):
        return None

    def close(self):
        self.closed = True


def _config():
    return StationPipelineConfig(
        input_uri="virtual:silence",
        icecast_host="127.0.0.1",
        icecast_port=11154,
        icecast_mount="/lofi",
        icecast_user="source",
        icecast_password="private-source-secret",
        local_output_enabled=False,
        output_device_id="",
        stream_codec_profile="aac_lc_128",
        stream_bitrate_kbps=128,
        icecast_stream_name="RadioTEDU Lo-Fi\r\nInjected: no",
        icecast_description="RadioTEDU",
        icecast_genre="Lo-Fi",
    )


def test_source_authentication_stays_in_socket_header_not_process_command():
    source_socket = FakeSocket()
    transport = IcecastSourceTransport(
        _config(), socket_factory=lambda *_args, **_kwargs: source_socket
    )
    try:
        assert source_socket.timeout is None
        handshake = source_socket.sent[0]
        expected = base64.b64encode(
            b"source:private-source-secret"
        )
        assert handshake.startswith(b"PUT /lofi HTTP/1.1\r\n")
        assert b"Authorization: Basic " + expected in handshake
        assert b"\r\nInjected:" not in handshake
        transport.send(b"encoded-audio")
        assert source_socket.sent[-1] == b"encoded-audio"
    finally:
        transport.close()
    assert source_socket.closed


def test_source_rejection_has_no_credential_echo():
    source_socket = FakeSocket(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
    with pytest.raises(IcecastSourceProtocolError) as exc:
        IcecastSourceTransport(
            _config(), socket_factory=lambda *_args, **_kwargs: source_socket
        )
    assert "private-source-secret" not in str(exc.value)
