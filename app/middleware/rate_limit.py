import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def allow(self, key: str, *, limit: int, window_sec: int) -> bool:
        safe_limit = max(1, int(limit))
        safe_window = max(1, int(window_sec))
        now = time.monotonic()
        cutoff = now - safe_window

        with self._lock:
            bucket = self._events[str(key)]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= safe_limit:
                return False
            bucket.append(now)
            return True
