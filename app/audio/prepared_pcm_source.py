"""Prepare a bounded successor without sending it to any output sink."""

import subprocess
import threading
import time


class _PrefixedPCMReader:
    def __init__(self, prefix: bytes, stream):
        self._prefix = memoryview(prefix)
        self._offset = 0
        self._stream = stream

    def read(self, size=-1):
        remaining = len(self._prefix) - self._offset
        if remaining:
            count = remaining if size < 0 else min(remaining, size)
            result = bytes(self._prefix[self._offset:self._offset + count])
            self._offset += count
            if self._offset == len(self._prefix):
                self._prefix = memoryview(b"")
                self._offset = 0
            if size < 0:
                return result + self._stream.read()
            return result
        return self._stream.read(size)

    def close(self):
        self._prefix = memoryview(b"")
        self._offset = 0
        self._stream.close()


class PreparedPCMSource:
    """One decoder, at most four seconds of PCM, and a bounded preparation time.

    The preparing thread is the only pipe reader until it publishes readiness.
    Taking the source transfers all buffered bytes, in order, to the usual
    programme pipe. Preparation has no sink or database ownership effects.
    """

    def __init__(self, key, command_factory, spawn, terminate, *,
                 capacity_bytes=4 * 48000 * 2 * 2, timeout_seconds=15.0):
        self.key = key
        self._command_factory = command_factory
        self._spawn = spawn
        self._terminate = terminate
        self._capacity = max(4096, int(capacity_bytes))
        self._lock = threading.Lock()
        self._cancelled = False
        self._taken = False
        self._process = None
        self._pcm = b""
        self._ready = False
        self._failed = False
        self.started = time.monotonic()
        self._timer = threading.Timer(max(0.1, timeout_seconds), self.cancel)
        self._timer.daemon = True
        self._thread = threading.Thread(target=self._prepare,
                                        name="station-next-pcm", daemon=True)
        self._timer.start()
        self._thread.start()

    def _prepare(self):
        process = None
        try:
            command = self._command_factory()
            with self._lock:
                if self._cancelled:
                    return
            process = self._spawn(command, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            with self._lock:
                cancelled = self._cancelled
                if not cancelled:
                    self._process = process
            if cancelled:
                self._terminate(process)
                process.stdout.close()
                return
            pcm = bytearray()
            while len(pcm) < self._capacity:
                with self._lock:
                    if self._cancelled:
                        return
                chunk = process.stdout.read(min(4096, self._capacity - len(pcm)))
                if not chunk:
                    # A pipe EOF can precede the process exit notification.
                    # A short finite clip is valid only after a clean exit.
                    if process.wait(timeout=1.0) != 0:
                        raise RuntimeError("successor decoder failed")
                    break
                pcm.extend(chunk)
            with self._lock:
                if not self._cancelled:
                    if not pcm:
                        raise RuntimeError("successor has no decoded audio")
                    self._pcm = bytes(pcm)
                    self._ready = True
        except Exception:
            with self._lock:
                self._failed = True
            self.cancel()

    def take(self, key):
        with self._lock:
            if (key != self.key or not self._ready or self._cancelled
                    or self._taken or self._failed):
                return None
            process = self._process
            if process.poll() not in (None, 0):
                return None
            # Publication occurs after the preparation reader stops reading.
            # Wrapping the pipe keeps the prefix exactly once and in order.
            process.stdout = _PrefixedPCMReader(self._pcm, process.stdout)
            self._pcm = b""
            self._taken = True
            self._timer.cancel()
            return process

    def cancel(self):
        with self._lock:
            if self._taken:
                return
            self._cancelled = True
            self._ready = False
            self._pcm = b""
            process = self._process
        self._timer.cancel()
        if process is not None:
            # Terminate before closing a pipe whose BufferedReader may hold a
            # lock in the preparation thread. Process exit unblocks that read.
            self._terminate(process)
            try:
                process.stdout.close()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            return {"ready": self._ready and not self._cancelled,
                    "buffered_pcm_bytes": len(self._pcm),
                    "failed": self._failed, "cancelled": self._cancelled,
                    "age_seconds": max(0.0, time.monotonic() - self.started)}
