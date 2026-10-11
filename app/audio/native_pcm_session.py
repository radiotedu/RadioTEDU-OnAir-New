"""Bounded, lossless 48 kHz microphone input; no decoder or encoder restart."""
import time

from app.audio.mic_session import MicSession


class NativePcmSession(MicSession):
    def __init__(self, station_id, *, max_buffer_bytes=38400, clock=time.monotonic):
        # MicSession owns the bounded FIFO and metering. It resolves FFmpeg but
        # this input never starts it: the browser already supplies native PCM.
        super().__init__(station_id, max_buffer_bytes=min(38400, max_buffer_bytes), require_decoder=False)
        self._clock = clock
        self._started_at = 0.0

    def start(self):
        with self._lock:
            self._buffer.clear()
            self._buffer_bytes = 0
        self._running = True
        self._receiving = False
        self._last_chunk_at = 0.0
        self._started_at = self._clock()

    def push_chunk(self, chunk):
        if not self._running:
            raise RuntimeError('microphone session is not running')
        data = bytes(chunk or b'')
        if not data or len(data) % 2 or len(data) > 9600:
            raise ValueError('PCM chunks must contain at most 100 ms of aligned mono samples')
        self._append_pcm(data)
        self._last_chunk_at = self._clock()
        self._receiving = True

    def stop(self):
        self._running = False
        self._receiving = False
        with self._lock:
            self._buffer.clear()
            self._buffer_bytes = 0

    def snapshot(self):
        result = super().snapshot()
        age = self._clock() - (self._last_chunk_at or self._started_at)
        result['receiving'] = bool(self._running and self._receiving and age < 0.75)
        result['expired'] = bool(self._running and age >= 3.0)
        return result
