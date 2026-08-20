from datetime import datetime, timedelta, timezone


class BruteForceProtection:
    def __init__(self):
        self.failed_attempts: dict[str, list[datetime]] = {}

    def _prune(self, identifier: str, now: datetime | None = None) -> list[datetime]:
        current = now or datetime.now(timezone.utc)
        window_start = current - timedelta(minutes=5)
        attempts = [
            ts
            for ts in self.failed_attempts.get(str(identifier), [])
            if ts >= window_start
        ]
        self.failed_attempts[str(identifier)] = attempts
        return attempts

    def check_allowed(self, identifier: str) -> bool:
        return len(self._prune(identifier)) < 5

    def record_failure(self, identifier: str) -> None:
        attempts = self._prune(identifier)
        attempts.append(datetime.now(timezone.utc))
        self.failed_attempts[str(identifier)] = attempts

    def record_success(self, identifier: str) -> None:
        self.failed_attempts.pop(str(identifier), None)
