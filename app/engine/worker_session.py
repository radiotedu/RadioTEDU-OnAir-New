"""Keep one scheduler connection per worker process, with bounded failure recovery."""


class StationWorkerSession:
    def __init__(self, factory):
        self._factory = factory
        self._worker = None

    def process_once(self):
        if self._worker is None:
            self._worker = self._factory()
        try:
            result = self._worker.process_once()
            # Previously each tick closed the connection, implicitly discarding
            # unfinished transactions. Keep that boundary without reconnecting
            # or leaving a write/read transaction pinned between scheduler ticks.
            if self._worker.conn.in_transaction:
                self._worker.conn.rollback()
            return result
        except Exception:
            try:
                self.close()
            except Exception:
                # Preserve the original tick failure for retry/backoff reporting.
                pass
            raise

    def close(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.conn.close()
