"""Exercise real encoder/source recovery against an owned loopback origin.

This does not disable a network adapter, contact production or reboot Windows.
Run with the same FFmpeg binary used by the live installation.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path


class LoopbackOrigin:
    def __init__(self):
        reserve = socket.socket()
        reserve.bind(("127.0.0.1", 0))
        self.port = reserve.getsockname()[1]
        reserve.close()
        self.stop_event = threading.Event()
        self.listener = None
        self.thread = None
        self.clients = []
        self.buffers = []
        self.lock = threading.Lock()

    def start(self):
        self.stop_event = threading.Event()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port))
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.thread.start()

    def _receive(self):
        while not self.stop_event.is_set():
            try:
                client, _address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.clients.append(client)
            client.settimeout(0.5)
            try:
                header = b""
                deadline = time.monotonic() + 5
                while b"\r\n\r\n" not in header and time.monotonic() < deadline:
                    header += client.recv(4096)
                    if len(header) > 16384:
                        raise RuntimeError("Test source header exceeded limit")
                if b"\r\n\r\n" not in header:
                    raise RuntimeError("Test source header missing")
                _headers, initial = header.split(b"\r\n\r\n", 1)
                client.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                buf = bytearray(initial)
                with self.lock:
                    self.buffers.append(buf)
                while not self.stop_event.is_set():
                    try:
                        block = client.recv(16384)
                    except socket.timeout:
                        continue
                    if not block:
                        break
                    with self.lock:
                        if len(buf) + len(block) > 2 * 1024 * 1024:
                            raise RuntimeError("Test capture exceeded limit")
                        buf.extend(block)
            except OSError:
                pass
            finally:
                client.close()

    def latest_size(self):
        with self.lock:
            return len(self.buffers[-1]) if self.buffers else 0

    def latest_capture(self):
        with self.lock:
            return bytes(self.buffers[-1]) if self.buffers else b""

    def stop(self):
        self.stop_event.set()
        if self.listener is not None:
            self.listener.close()
        for client in self.clients:
            try:
                # RST promptly exposes a dead origin to a real socket writer.
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2)


def wait_for(predicate, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError(message)


def verify(ffmpeg: str, output: Path):
    from app.audio.gst_pipeline import StationPipelineConfig
    from app.audio.icecast_audio_sink import IcecastAudioSink

    origin = LoopbackOrigin()
    def spawn(command, **kwargs):
        return subprocess.Popen(
            command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs
        )

    sink = IcecastAudioSink(ffmpeg, spawn)
    cfg = StationPipelineConfig(
        input_uri="silence://", icecast_host="127.0.0.1", icecast_port=origin.port,
        icecast_mount="/durability-loopback-test", icecast_user="source",
        icecast_password=secrets.token_urlsafe(24), local_output_enabled=False,
        output_device_id="", stream_codec_profile="aac_low_192", stream_bitrate_kbps=192,
    )
    stop = threading.Event()
    feed_errors = []

    def feed():
        frame = 0
        while not stop.is_set():
            samples = []
            for n in range(960):
                value = int(5000 * math.sin(2 * math.pi * 440 * (frame + n) / 48000))
                samples.extend((value, value))
            frame += 960
            if not sink.write_pcm(struct.pack("<" + "h" * len(samples), *samples)):
                if not stop.is_set():
                    feed_errors.append("PCM input rejected")
                return
            stop.wait(0.02)

    feeder = threading.Thread(target=feed, daemon=True)
    started = time.monotonic()
    try:
        sink.ensure_started(cfg)  # No test origin exists yet: equivalent to offline boot.
        boot_start_seconds = time.monotonic() - started
        feeder.start()
        time.sleep(6)
        assert sink.health_snapshot()["network_error_count"] > 0
        origin.start()
        wait_for(lambda: origin.latest_size() >= 32768, 60, "Offline boot did not reconnect")
        first_capture = origin.latest_capture()
        first_connections = len(origin.buffers)
        errors_before_disconnect = sink.health_snapshot()["network_error_count"]
        origin.stop()
        wait_for(
            lambda: sink.health_snapshot()["network_error_count"] > errors_before_disconnect,
            20, "Established source loss was not detected",
        )
        time.sleep(6)
        origin.start()
        wait_for(
            lambda: len(origin.buffers) > first_connections and origin.latest_size() >= 32768,
            60, "Established source did not reconnect after origin returned",
        )
        health = sink.health_snapshot()
        assert not feed_errors, feed_errors
        assert health["dropped_pcm_chunks"] == 0
        assert not health["profile_fallback_active"]
        assert health["effective_stream_codec_profile"] == cfg.stream_codec_profile
        assert health["requested_stream_codec_profile"] == cfg.stream_codec_profile
        captures = [first_capture, origin.latest_capture()]
    except BaseException:
        import faulthandler
        diagnostic = {
            "health": sink.health_snapshot(), "originConnections": len(origin.buffers),
            "originCaptureSizes": [len(b) for b in origin.buffers],
            "feederAlive": feeder.is_alive(), "feedErrors": feed_errors,
        }
        (output / "network-recovery-diagnostic.json").write_text(json.dumps(diagnostic, indent=2))
        with (output / "network-recovery-thread-stacks.txt").open("w") as trace:
            faulthandler.dump_traceback(file=trace, all_threads=True)
        raise
    finally:
        stop.set()
        sink.stop()
        if feeder.ident is not None:
            feeder.join(timeout=3)
        origin.stop()

    decoded = []
    for number, capture in enumerate(captures, 1):
        encoded = output / f"loopback-source-{number}.bin"
        encoded.write_bytes(capture)
        process = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-i", str(encoded), "-t", "1", "-vn",
             "-ar", "48000", "-ac", "2", "-f", "s16le", "pipe:1"],
            capture_output=True, timeout=20, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        samples = array.array("h", process.stdout)
        rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))
        assert len(samples) >= 48000 and rms > 0, "Recovered source did not contain decoded audio"
        decoded.append({"samples": len(samples), "rms": round(rms, 3)})
    return {
        "passed": True, "productionNetworkChanged": False,
        "offlineBootRecovered": True, "establishedOutageRecovered": True,
        "bootSinkStartSeconds": round(boot_start_seconds, 3),
        "requestedProfile": cfg.stream_codec_profile,
        "recoveredProfile": health["effective_stream_codec_profile"],
        "bitrateKbps": cfg.stream_bitrate_kbps,
        "profileFallback": health["profile_fallback_active"],
        "droppedPcmChunks": health["dropped_pcm_chunks"], "decoded": decoded,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Imports must not accidentally inherit an operator's production database.
    os.environ["CLEANROOM_DATA_ROOT"] = str(args.output_dir / "isolated-state")
    os.environ["CLEANROOM_DB_PATH"] = str(args.output_dir / "isolated-state/test.db")
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    result = verify(args.ffmpeg, args.output_dir)
    (args.output_dir / "network-recovery-result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
