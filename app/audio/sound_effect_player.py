import logging
import math
import struct
import subprocess
import threading
import time
from collections import deque

from app.runtime_paths import resolve_binary

logger = logging.getLogger("cleanroom.soundboard")

PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 1
PCM_CHUNK_BYTES = 4096
PCM_MAX_BUFFER_SECONDS = 5
PCM_MAX_BUFFER_BYTES = (
    PCM_SAMPLE_RATE * PCM_CHANNELS * 2 * PCM_MAX_BUFFER_SECONDS
)


class SoundEffectSlot:
    """A single playing sound effect — FFmpeg decodes file to PCM via reader thread."""

    def __init__(self, item_id: int, name: str, file_path: str, gain_db: float = 0.0) -> None:
        self.item_id = int(item_id)
        self.name = str(name)
        self.gain_db = float(gain_db)
        self._gain_linear = math.pow(10.0, self.gain_db / 20.0) if self.gain_db != 0.0 else 1.0
        self._lock = threading.Lock()
        self._buffer: deque[bytes] = deque()
        self._buffer_bytes = 0
        self._finished = False
        self._process_exited = False
        self._started_at = time.monotonic()

        ffmpeg_bin = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg") or "ffmpeg"
        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-i", str(file_path),
            "-f", "s16le", "-ar", str(PCM_SAMPLE_RATE), "-ac", str(PCM_CHANNELS),
            "pipe:1",
        ]
        self._process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, name=f"sfx-slot-{self.item_id}", daemon=True,
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            self._process_exited = True
            return
        try:
            while not self._finished:
                with self._lock:
                    buffer_room = max(
                        0,
                        PCM_MAX_BUFFER_BYTES - self._buffer_bytes,
                    )
                if buffer_room <= 0:
                    # Let the FFmpeg stdout pipe provide natural backpressure.
                    # Without this cap a long effect is decoded as fast as the
                    # CPU allows and retained entirely in RAM.
                    time.sleep(0.01)
                    continue
                chunk = proc.stdout.read(min(PCM_CHUNK_BYTES, buffer_room))
                if not chunk:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.005)
                    continue
                with self._lock:
                    self._buffer.append(chunk)
                    self._buffer_bytes += len(chunk)
        except Exception:
            pass
        finally:
            self._process_exited = True

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
                else:
                    output.extend(chunk[:need])
                    self._buffer[0] = chunk[need:]
                    self._buffer_bytes -= need
        # Apply gain
        if self._gain_linear != 1.0 and len(output) >= 2:
            samples = struct.unpack(f"<{len(output) // 2}h", bytes(output))
            gained = struct.pack(
                f"<{len(samples)}h",
                *(max(-32768, min(32767, int(round(s * self._gain_linear)))) for s in samples),
            )
            output = bytearray(gained)
        # Pad with silence if needed
        if len(output) < requested:
            output.extend(b"\x00" * (requested - len(output)))
        return bytes(output)

    @property
    def finished(self) -> bool:
        if self._finished:
            return True
        if self._process_exited:
            with self._lock:
                if self._buffer_bytes == 0:
                    self._finished = True
                    return True
        return False

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started_at

    def stop(self) -> None:
        self._finished = True
        if self._process is not None:
            try:
                self._process.kill()
                self._process.wait(timeout=2)
            except Exception:
                pass
            self._process = None
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)


class SoundEffectPlayer:
    """Manages N concurrent effect slots for a station. Pre-mixes all active slots."""

    def __init__(self, station_id: int) -> None:
        self.station_id = int(station_id)
        self._slots: list[SoundEffectSlot] = []
        self._lock = threading.Lock()

    def _create_slot(self, item: dict) -> SoundEffectSlot:
        return SoundEffectSlot(
            item_id=int(item["id"]),
            name=str(item["name"]),
            file_path=str(item["file_path"]),
            gain_db=float(item.get("gain_db", 0.0)),
        )

    def play(self, item: dict) -> None:
        slot = self._create_slot(item)
        with self._lock:
            self._slots.append(slot)
        logger.info("Playing effect %d (%s) on station %d", slot.item_id, slot.name, self.station_id)

    def stop(self, item_id: int | None = None) -> None:
        with self._lock:
            if item_id is None:
                for s in self._slots:
                    s.stop()
                self._slots.clear()
            else:
                target = [s for s in self._slots if s.item_id == int(item_id)]
                for s in target:
                    s.stop()
                self._slots = [s for s in self._slots if s.item_id != int(item_id)]

    @property
    def has_active(self) -> bool:
        with self._lock:
            active = [slot for slot in self._slots if not slot.finished]
            self._slots = active
            return bool(active)

    def read_pcm(self, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        if requested == 0:
            return b""
        with self._lock:
            active = [s for s in self._slots if not s.finished]
            self._slots = active
        if not active:
            return b"\x00" * requested
        # Read PCM from each slot
        slot_pcms = [s.read_pcm(requested) for s in active]
        # Pre-mix: sum samples, clamp to int16
        sample_count = requested // 2
        mixed = bytearray(requested)
        for pcm in slot_pcms:
            if len(pcm) < requested:
                pcm = pcm + b"\x00" * (requested - len(pcm))
            for i in range(sample_count):
                offset = i * 2
                existing = int.from_bytes(mixed[offset:offset + 2], "little", signed=True)
                incoming = int.from_bytes(pcm[offset:offset + 2], "little", signed=True)
                total = max(-32768, min(32767, existing + incoming))
                mixed[offset:offset + 2] = total.to_bytes(2, "little", signed=True)
        return bytes(mixed)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len([s for s in self._slots if not s.finished])

    def snapshot(self) -> dict:
        with self._lock:
            items = [
                {"item_id": s.item_id, "name": s.name, "elapsed_s": round(s.elapsed_s, 1)}
                for s in self._slots if not s.finished
            ]
        return {"active_items": items, "count": len(items)}
