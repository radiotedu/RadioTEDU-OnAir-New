from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.audio.shoutcast_audio_sink import ShoutcastAudioSink
from app.audio.gst_pipeline import StationPipelineConfig


def _ffmpeg_path() -> str | None:
    candidates = [
        shutil.which("ffmpeg.exe"),
        shutil.which("ffmpeg"),
        str(Path(__file__).resolve().parents[2] / "tools" / "bin" / "ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class _ShoutcastSourceFixture:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = int(self.listener.getsockname()[1])
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.headers = b""
        self.received_bytes = 0
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read_until(self, conn: socket.socket, marker: bytes, timeout: float = 5.0) -> bytes:
        conn.settimeout(timeout)
        data = bytearray()
        while marker not in data and len(data) < 16 * 1024:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _run(self) -> None:
        conn = None
        try:
            self.listener.settimeout(8.0)
            conn, _ = self.listener.accept()
            credential = self._read_until(conn, b"\r\n")
            assert credential == b"source-password\r\n"
            conn.sendall(b"OK2\r\n")
            self.headers = self._read_until(conn, b"\r\n\r\n")
            self.ready.set()
            conn.settimeout(0.5)
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                try:
                    chunk = conn.recv(64 * 1024)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                self.received_bytes += len(chunk)
        except BaseException as exc:  # surfaced to the test after teardown
            self.error = exc
        finally:
            self.finished.set()
            if conn is not None:
                conn.close()
            self.listener.close()

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass
        self._thread.join(timeout=3)


def test_real_shoutcast_source_receives_encoded_audio_for_short_soak() -> None:
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        pytest.skip("FFmpeg executable is not available")

    server = _ShoutcastSourceFixture()
    server.start()
    cfg = StationPipelineConfig(
        input_uri="virtual:silence",
        icecast_host="127.0.0.1",
        icecast_port=server.port,
        icecast_mount="/radiotedu-real",
        icecast_user="source",
        icecast_password="source-password",
        local_output_enabled=False,
        output_device_id="",
        stream_codec_profile="mp3_128",
        stream_bitrate_kbps=128,
        stream_title="RadioTEDU integration",
        stream_artist="Automated fixture",
        icecast_stream_name="RadioTEDU integration",
        icecast_description="local source fixture",
        source_protocol="shoutcast",
    )
    sink = ShoutcastAudioSink(
        ffmpeg,
        subprocess.Popen,
        connect_timeout_sec=2,
        handshake_timeout_sec=2,
        write_timeout_sec=2,
    )
    try:
        sink.ensure_started(cfg)
        deadline = time.monotonic() + 5
        pcm = b"\x00\x10\x00\xf0" * 4096
        while time.monotonic() < deadline and not server.ready.is_set():
            sink.write_pcm(pcm)
            time.sleep(0.02)
        assert server.ready.wait(2), server.error
        for _ in range(140):
            sink.write_pcm(pcm)
            time.sleep(0.02)
        time.sleep(0.5)
        snapshot = sink.health_snapshot()
        assert snapshot["handshake_accepted"] is True
        assert snapshot["network_failed"] is False
        assert snapshot["encoded_bytes_sent"] > 0
        assert server.received_bytes > 0
        assert b"icy-name:RadioTEDU integration" in server.headers
        assert b"icy-br:128" in server.headers
        assert b"source-password" not in server.headers
    finally:
        sink.stop()
        server.close()
    assert server.error is None
