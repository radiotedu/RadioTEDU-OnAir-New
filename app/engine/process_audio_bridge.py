from __future__ import annotations

import mmap
import os
import struct
import threading
import time
from pathlib import Path


_MAGIC = b"RTABRDG1"
_VERSION = 1
_HEADER_BYTES = 128
_MIC_CAPACITY = 384_000
_GUEST_CAPACITY = 384_000
_PROGRAM_CAPACITY = 1_536_000
_FILE_BYTES = _HEADER_BYTES + _MIC_CAPACITY + _GUEST_CAPACITY + _PROGRAM_CAPACITY
_STALE_SECONDS = 1.0
_PUMP_BYTES = 1_920

_OFF_MAGIC = 0
_OFF_VERSION = 8
_OFF_MIC_CAPACITY = 12
_OFF_GUEST_CAPACITY = 16
_OFF_PROGRAM_CAPACITY = 20
_OFF_MIC_WRITE = 24
_OFF_MIC_READ = 32
_OFF_GUEST_WRITE = 40
_OFF_GUEST_READ = 48
_OFF_PROGRAM_WRITE = 56
_OFF_PROGRAM_READ = 64
_OFF_UPDATED_EPOCH = 72
_OFF_FLAGS = 80
_OFF_ACTIVE_USER_ID = 88
_OFF_LEVEL_DB = 96
_OFF_PEAK_DB = 100

_FLAG_LIVE_ENABLED = 1 << 0
_FLAG_TRANSMITTING = 1 << 1
_FLAG_RECEIVING = 1 << 2
_FLAG_GUEST_ON_AIR = 1 << 3


def _read_u64(mapping: mmap.mmap, offset: int) -> int:
    return int(struct.unpack_from("<Q", mapping, offset)[0])


def _write_u64(mapping: mmap.mmap, offset: int, value: int) -> None:
    struct.pack_into("<Q", mapping, offset, max(0, int(value)))


def _ring_write(
    mapping: mmap.mmap,
    *,
    start: int,
    capacity: int,
    write_offset: int,
    read_offset: int,
    payload: bytes,
) -> int:
    data = bytes(payload or b"")
    if not data:
        return 0
    if len(data) > capacity:
        data = data[-capacity:]
    write_counter = _read_u64(mapping, write_offset)
    read_counter = _read_u64(mapping, read_offset)
    next_write = write_counter + len(data)
    if next_write - read_counter > capacity:
        _write_u64(mapping, read_offset, next_write - capacity)
    position = write_counter % capacity
    first = min(len(data), capacity - position)
    mapping[start + position : start + position + first] = data[:first]
    if first < len(data):
        mapping[start : start + len(data) - first] = data[first:]
    _write_u64(mapping, write_offset, next_write)
    return len(data)


def _ring_read(
    mapping: mmap.mmap,
    *,
    start: int,
    capacity: int,
    write_offset: int,
    read_offset: int,
    requested: int,
    pad: bool,
) -> bytes:
    wanted = max(0, int(requested))
    if wanted <= 0:
        return b""
    write_counter = _read_u64(mapping, write_offset)
    read_counter = _read_u64(mapping, read_offset)
    if write_counter < read_counter:
        read_counter = write_counter
    if write_counter - read_counter > capacity:
        read_counter = write_counter - capacity
    available = min(wanted, write_counter - read_counter)
    position = read_counter % capacity
    first = min(available, capacity - position)
    output = bytearray(mapping[start + position : start + position + first])
    if first < available:
        output.extend(mapping[start : start + available - first])
    _write_u64(mapping, read_offset, read_counter + available)
    if pad and len(output) < wanted:
        output.extend(b"\x00" * (wanted - len(output)))
    return bytes(output)


class _BridgeMapping:
    def __init__(self, path: Path, *, create: bool):
        self.path = Path(path).expanduser().resolve()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if self.path.exists() else ("w+b" if create else "r+b")
        self._file = self.path.open(mode)
        if create and self.path.stat().st_size != _FILE_BYTES:
            self._file.truncate(_FILE_BYTES)
            self._file.flush()
        if self.path.stat().st_size != _FILE_BYTES:
            self._file.close()
            raise RuntimeError("station audio bridge size is invalid")
        self.mapping = mmap.mmap(self._file.fileno(), _FILE_BYTES, access=mmap.ACCESS_WRITE)
        if bytes(self.mapping[_OFF_MAGIC : _OFF_MAGIC + 8]) != _MAGIC:
            if not create:
                self.close()
                raise RuntimeError("station audio bridge header is invalid")
            self.mapping[:] = b"\x00" * _FILE_BYTES
            self.mapping[_OFF_MAGIC : _OFF_MAGIC + 8] = _MAGIC
            struct.pack_into("<I", self.mapping, _OFF_VERSION, _VERSION)
            struct.pack_into("<I", self.mapping, _OFF_MIC_CAPACITY, _MIC_CAPACITY)
            struct.pack_into("<I", self.mapping, _OFF_GUEST_CAPACITY, _GUEST_CAPACITY)
            struct.pack_into("<I", self.mapping, _OFF_PROGRAM_CAPACITY, _PROGRAM_CAPACITY)
            self.mapping.flush()
        version = int(struct.unpack_from("<I", self.mapping, _OFF_VERSION)[0])
        capacities = (
            int(struct.unpack_from("<I", self.mapping, _OFF_MIC_CAPACITY)[0]),
            int(struct.unpack_from("<I", self.mapping, _OFF_GUEST_CAPACITY)[0]),
            int(struct.unpack_from("<I", self.mapping, _OFF_PROGRAM_CAPACITY)[0]),
        )
        if version != _VERSION or capacities != (
            _MIC_CAPACITY,
            _GUEST_CAPACITY,
            _PROGRAM_CAPACITY,
        ):
            self.close()
            raise RuntimeError("station audio bridge version is unsupported")

    def close(self) -> None:
        mapping = getattr(self, "mapping", None)
        self.mapping = None
        if mapping is not None:
            try:
                mapping.close()
            except (BufferError, OSError):
                pass
        handle = getattr(self, "_file", None)
        self._file = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


class ProcessAudioBridgeClient:
    """Worker-side SPSC PCM bridge that survives backend process replacement."""

    def __init__(self, path: Path):
        self._bridge = _BridgeMapping(path, create=False)
        self._mapping = self._bridge.mapping
        self._mic_start = _HEADER_BYTES
        self._guest_start = self._mic_start + _MIC_CAPACITY
        self._program_start = self._guest_start + _GUEST_CAPACITY

    def _status(self) -> tuple[int, float, int, float, float]:
        flags = int(struct.unpack_from("<I", self._mapping, _OFF_FLAGS)[0])
        updated = float(
            struct.unpack_from("<d", self._mapping, _OFF_UPDATED_EPOCH)[0]
        )
        active_user_id = int(
            struct.unpack_from("<q", self._mapping, _OFF_ACTIVE_USER_ID)[0]
        )
        level_db = float(struct.unpack_from("<f", self._mapping, _OFF_LEVEL_DB)[0])
        peak_db = float(struct.unpack_from("<f", self._mapping, _OFF_PEAK_DB)[0])
        if not updated or max(0.0, time.time() - updated) > _STALE_SECONDS:
            return 0, updated, 0, -60.0, -60.0
        return flags, updated, active_user_id, level_db, peak_db

    def snapshot(self, _station_id: int) -> dict:
        flags, _updated, active_user_id, level_db, peak_db = self._status()
        transmitting = bool(flags & _FLAG_TRANSMITTING)
        return {
            "active_user": (
                {"id": active_user_id, "username": "process-bridge"}
                if active_user_id > 0
                else None
            ),
            "buffer_bytes": max(
                0,
                _read_u64(self._mapping, _OFF_MIC_WRITE)
                - _read_u64(self._mapping, _OFF_MIC_READ),
            ),
            "level_db": level_db,
            "live_input_enabled": bool(flags & _FLAG_LIVE_ENABLED),
            "peak_db": peak_db,
            "receiving": bool(flags & _FLAG_RECEIVING),
            "transmitting": transmitting,
        }

    def read_pcm(self, _station_id: int, requested: int) -> bytes:
        flags, *_rest = self._status()
        if not bool(flags & _FLAG_TRANSMITTING):
            return b"\x00" * max(0, int(requested))
        return _ring_read(
            self._mapping,
            start=self._mic_start,
            capacity=_MIC_CAPACITY,
            write_offset=_OFF_MIC_WRITE,
            read_offset=_OFF_MIC_READ,
            requested=requested,
            pad=True,
        )

    def has_on_air(self, _station_id: int) -> bool:
        flags, *_rest = self._status()
        return bool(flags & _FLAG_GUEST_ON_AIR)

    def read_on_air_pcm(self, _station_id: int, requested: int) -> bytes:
        if not self.has_on_air(_station_id):
            return b"\x00" * max(0, int(requested))
        return _ring_read(
            self._mapping,
            start=self._guest_start,
            capacity=_GUEST_CAPACITY,
            write_offset=_OFF_GUEST_WRITE,
            read_offset=_OFF_GUEST_READ,
            requested=requested,
            pad=True,
        )

    def publish_program_pcm(
        self,
        _station_id: int,
        program_stereo_pcm: bytes,
        *,
        voice_gain: float = 1.0,
    ) -> None:
        del voice_gain
        _ring_write(
            self._mapping,
            start=self._program_start,
            capacity=_PROGRAM_CAPACITY,
            write_offset=_OFF_PROGRAM_WRITE,
            read_offset=_OFF_PROGRAM_READ,
            payload=program_stereo_pcm,
        )

    def close(self) -> None:
        self._bridge.close()


class ProcessAudioBridgeHost:
    """Backend-side bounded audio pump for one isolated station process."""

    def __init__(
        self,
        path: Path,
        station_id: int,
        *,
        live_mic_registry=None,
        guest_audio_registry=None,
        live_settings_provider=None,
    ):
        self.station_id = int(station_id)
        self.live_mic_registry = live_mic_registry
        self.guest_audio_registry = guest_audio_registry
        self.live_settings_provider = live_settings_provider
        self._bridge = _BridgeMapping(path, create=True)
        self._mapping = self._bridge.mapping
        self._mic_start = _HEADER_BYTES
        self._guest_start = self._mic_start + _MIC_CAPACITY
        self._program_start = self._guest_start + _GUEST_CAPACITY
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _snapshot_active_user_id(snapshot: dict) -> int:
        active = snapshot.get("active_user")
        if isinstance(active, dict):
            try:
                return max(0, int(active.get("id") or 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def _write_status(self, snapshot: dict, guest_on_air: bool) -> None:
        flags = 0
        if bool(snapshot.get("live_input_enabled")):
            flags |= _FLAG_LIVE_ENABLED
        if bool(snapshot.get("transmitting") or snapshot.get("active_user")):
            flags |= _FLAG_TRANSMITTING
        if bool(snapshot.get("receiving")):
            flags |= _FLAG_RECEIVING
        if guest_on_air:
            flags |= _FLAG_GUEST_ON_AIR
        struct.pack_into("<I", self._mapping, _OFF_FLAGS, flags)
        struct.pack_into(
            "<q",
            self._mapping,
            _OFF_ACTIVE_USER_ID,
            self._snapshot_active_user_id(snapshot),
        )
        struct.pack_into(
            "<f", self._mapping, _OFF_LEVEL_DB, float(snapshot.get("level_db", -60.0))
        )
        struct.pack_into(
            "<f", self._mapping, _OFF_PEAK_DB, float(snapshot.get("peak_db", -60.0))
        )
        struct.pack_into("<d", self._mapping, _OFF_UPDATED_EPOCH, time.time())

    def _discard_input(self, write_offset: int, read_offset: int) -> None:
        _write_u64(self._mapping, read_offset, _read_u64(self._mapping, write_offset))

    def _pump_once(self) -> None:
        snapshot = {}
        if self.live_mic_registry is not None:
            try:
                snapshot = dict(
                    self.live_mic_registry.snapshot(self.station_id) or {}
                )
            except Exception:
                snapshot = {}
        mic_active = bool(snapshot.get("transmitting") or snapshot.get("active_user"))
        if mic_active and self.live_mic_registry is not None:
            try:
                mic = self.live_mic_registry.read_pcm(self.station_id, _PUMP_BYTES)
            except Exception:
                mic = b""
            _ring_write(
                self._mapping,
                start=self._mic_start,
                capacity=_MIC_CAPACITY,
                write_offset=_OFF_MIC_WRITE,
                read_offset=_OFF_MIC_READ,
                payload=mic,
            )
        else:
            self._discard_input(_OFF_MIC_WRITE, _OFF_MIC_READ)

        guest_on_air = False
        if self.guest_audio_registry is not None:
            try:
                guest_on_air = bool(
                    self.guest_audio_registry.has_on_air(self.station_id)
                )
            except Exception:
                guest_on_air = False
        if guest_on_air:
            try:
                guest = self.guest_audio_registry.read_on_air_pcm(
                    self.station_id, _PUMP_BYTES
                )
            except Exception:
                guest = b""
            _ring_write(
                self._mapping,
                start=self._guest_start,
                capacity=_GUEST_CAPACITY,
                write_offset=_OFF_GUEST_WRITE,
                read_offset=_OFF_GUEST_READ,
                payload=guest,
            )
        else:
            self._discard_input(_OFF_GUEST_WRITE, _OFF_GUEST_READ)

        program = _ring_read(
            self._mapping,
            start=self._program_start,
            capacity=_PROGRAM_CAPACITY,
            write_offset=_OFF_PROGRAM_WRITE,
            read_offset=_OFF_PROGRAM_READ,
            requested=38_400,
            pad=False,
        )
        if program and self.guest_audio_registry is not None:
            settings = {}
            if callable(self.live_settings_provider):
                try:
                    settings = dict(self.live_settings_provider(self.station_id) or {})
                except Exception:
                    settings = {}
            try:
                self.guest_audio_registry.publish_program_pcm(
                    self.station_id,
                    program,
                    voice_gain=float(settings.get("mic_gain", 1.0)),
                )
            except Exception:
                pass
        self._write_status(snapshot, guest_on_air)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._pump_once()
            except Exception:
                self._write_status({}, False)
            remaining = max(0.001, 0.02 - (time.monotonic() - started))
            self._stop.wait(remaining)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"station-audio-bridge-{self.station_id}",
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        try:
            self._write_status({}, False)
        except Exception:
            pass
        self._bridge.close()
