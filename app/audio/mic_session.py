import math
import subprocess
import threading
import time
from collections import deque

from app.runtime_paths import resolve_binary

PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_CHUNK_BYTES = 4096


class MicSession:
    def __init__(self, station_id: int, max_buffer_bytes: int = 96000) -> None:
        self.station_id = int(station_id)
        self.max_buffer_bytes = max(PCM_CHUNK_BYTES, int(max_buffer_bytes))
        self.ffmpeg_bin = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        self._process = None
        self._reader_thread = None
        self._buffer = deque()
        self._buffer_bytes = 0
        self._lock = threading.Lock()
        self._running = False
        self._receiving = False
        self._last_chunk_at = 0.0
        self._level_db = -60.0
        self._peak_db = -60.0
        self._last_error = ""

    @property
    def running(self) -> bool:
        return bool(self._running)

    def _decoder_cmd(self) -> list[str]:
        return [
            str(self.ffmpeg_bin),
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+nobuffer",
            "-flags",
            "low_delay",
            "-i",
            "pipe:0",
            "-vn",
            "-f",
            "s16le",
            "-ar",
            str(PCM_SAMPLE_RATE),
            "-ac",
            str(PCM_CHANNELS),
            "pipe:1",
        ]

    def _append_pcm(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._buffer.append(bytes(chunk))
            self._buffer_bytes += len(chunk)
            while self._buffer_bytes > self.max_buffer_bytes and self._buffer:
                dropped = self._buffer.popleft()
                self._buffer_bytes -= len(dropped)
        self._update_levels(chunk)

    def _update_levels(self, chunk: bytes) -> None:
        if len(chunk) < 2:
            return
        sample_count = len(chunk) // 2
        if sample_count <= 0:
            return
        values = memoryview(chunk).cast("h")
        peak = max(abs(int(sample)) for sample in values) if values else 0
        if peak <= 0:
            self._peak_db = -60.0
            self._level_db = -60.0
            return
        rms = math.sqrt(sum(int(sample) * int(sample) for sample in values) / sample_count)
        self._peak_db = max(-60.0, 20.0 * math.log10(peak / 32767.0))
        self._level_db = max(-60.0, 20.0 * math.log10(max(rms, 1.0) / 32767.0))

    def _read_stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while self._running:
                chunk = process.stdout.read(PCM_CHUNK_BYTES)
                if not chunk:
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                    continue
                self._append_pcm(chunk)
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._process = subprocess.Popen(
            self._decoder_cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._buffer.clear()
        self._buffer_bytes = 0
        self._running = True
        self._receiving = False
        self._last_error = ""
        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            name=f"mic-session-{self.station_id}",
            daemon=True,
        )
        self._reader_thread.start()

    def push_chunk(self, chunk: bytes) -> None:
        if not self._running:
            raise RuntimeError("mic session is not running")
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("decoder stdin unavailable")
        data = bytes(chunk or b"")
        if not data:
            return
        try:
            process.stdin.write(data)
            process.stdin.flush()
            self._receiving = True
            self._last_chunk_at = time.monotonic()
        except Exception as exc:
            self._last_error = str(exc)
            raise

    def read_pcm(self, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        if requested == 0:
            return b""
        output = bytearray()
        with self._lock:
            while self._buffer and len(output) < requested:
                chunk = self._buffer[0]
                need = requested - len(output)
                if len(chunk) <= need:
                    output.extend(self._buffer.popleft())
                    self._buffer_bytes -= len(chunk)
                    continue
                output.extend(chunk[:need])
                self._buffer[0] = chunk[need:]
                self._buffer_bytes -= need
        if len(output) < requested:
            output.extend(b"\x00" * (requested - len(output)))
        return bytes(output)

    def stop(self) -> None:
        self._running = False
        process = self._process
        self._process = None
        if process is not None:
            stdin = getattr(process, "stdin", None)
            stdout = getattr(process, "stdout", None)
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:
                    pass
            if stdout is not None:
                try:
                    stdout.close()
                except Exception:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except Exception:
                    process.kill()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None
        self._receiving = False

    def snapshot(self) -> dict:
        return {
            "station_id": self.station_id,
            "running": bool(self._running),
            "receiving": bool(self._receiving),
            "buffer_bytes": int(self._buffer_bytes),
            "level_db": float(self._level_db),
            "peak_db": float(self._peak_db),
            "last_chunk_at": float(self._last_chunk_at or 0.0),
            "last_error": str(self._last_error or ""),
        }
