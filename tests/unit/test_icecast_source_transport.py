from __future__ import annotations

import base64
import socket

import pytest

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_source_transport import (
    DEFAULT_SOURCE_WRITE_TIMEOUT_SECONDS,
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


class DelayedHandshakeSocket(FakeSocket):
    def recv(self, _size):
        raise socket.timeout("origin waits for source body")


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
        assert DEFAULT_SOURCE_WRITE_TIMEOUT_SECONDS == 10.0
        assert source_socket.timeout == 10.0
        handshake = source_socket.sent[0]
        expected = base64.b64encode(
            b"source:private-source-secret"
        )
        assert handshake.startswith(b"PUT /lofi HTTP/1.1\r\n")
        assert b"Authorization: Basic " + expected in handshake
        assert b"Expect: 100-continue\r\n" in handshake
        assert b"Connection: close\r\n" in handshake
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


def test_source_can_start_when_origin_defers_200_until_first_body_bytes():
    source_socket = DelayedHandshakeSocket()
    transport = IcecastSourceTransport(
        _config(), socket_factory=lambda *_args, **_kwargs: source_socket
    )
    try:
        transport.send(b"encoded-audio")
        assert source_socket.sent[-1] == b"encoded-audio"
        assert source_socket.timeout == 10.0
    finally:
        transport.close()


def test_source_can_start_after_http_100_continue():
    source_socket = FakeSocket(b"HTTP/1.1 100 Continue\r\n\r\n")
    transport = IcecastSourceTransport(
        _config(), socket_factory=lambda *_args, **_kwargs: source_socket
    )
    try:
        transport.send(b"encoded-audio")
        assert source_socket.sent[-1] == b"encoded-audio"
    finally:
        transport.close()


def test_established_source_has_a_bounded_write_deadline():
    # Allow temporary TinyIce backpressure while bounding a stuck sendall.
    assert DEFAULT_SOURCE_WRITE_TIMEOUT_SECONDS == 10.0

    source_socket = FakeSocket()
    transport = IcecastSourceTransport(
        _config(), socket_factory=lambda *_args, **_kwargs: source_socket
    )
    try:
        assert source_socket.timeout == 10.0
        transport.send(b"programme-before-pause")
        transport.send(b"programme-after-pause")
        assert source_socket.sent[-1] == b"programme-after-pause"
        assert not source_socket.closed
    finally:
        transport.close()


def test_explicit_source_write_timeout_is_still_supported():
    source_socket = FakeSocket()
    transport = IcecastSourceTransport(
        _config(), socket_factory=lambda *_args, **_kwargs: source_socket,
        write_timeout_sec=2.5,
    )
    try:
        assert source_socket.timeout == 2.5
    finally:
        transport.close()
