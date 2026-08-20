# RadioTEDU OnAir Reliability Model

RadioTEDU OnAir is engineered for deterministic, operator-controlled broadcast
continuity. This document describes implemented guarantees and operational
limits. It is not a military, NATO, safety-integrity, or availability
certification.

## Implemented guarantees

- Operator stop remains authoritative; watchdogs do not restart an intentionally
  stopped station.
- Output degradation is handled before last-resort restart.
- Icecast reconnects preserve playout state and track offset.
- Silence-floor continuity prevents a connected source from dropping during
  short program gaps.
- Worker failures use bounded exponential backoff.
- SQLite uses WAL, foreign-key enforcement, a 30-second busy timeout, bounded
  WAL checkpoints, and `FULL` synchronous commits by default.
- Critical JSON ledgers use unique temporary files, file flush plus `fsync`,
  atomic replacement, and a last-valid backup generation.
- Corrupt primary JSON state is read from its last valid backup.
- Liveness and readiness are separate:
  - `GET /api/health/live` proves the HTTP process can answer.
  - `GET /api/health/ready` verifies database integrity and durable settings.
- The operator Settings panel displays database integrity, durability mode, and
  free-storage percentage.

## Failure semantics

| Failure | Behavior |
|---|---|
| Local speaker branch fails | Continue required Icecast output; report degraded |
| Icecast output fails | Preserve program state; reconnect with bounded retries |
| Worker tick raises | Record error and back off up to 120 seconds |
| Primary JSON ledger is corrupt | Load the last valid backup |
| Database integrity fails | Readiness returns HTTP 503 and reports critical |
| Storage is critically low | Readiness reports critical |
| Operator stops a station | Automation leaves it stopped |

## Operational checks

1. Monitor `/api/health/live` every 10 seconds.
2. Monitor `/api/health/ready` every 30 seconds.
3. Alert immediately on readiness HTTP 503.
4. Alert when free storage falls below 10 percent or 2 GiB.
5. Keep at least one tested offline database backup.
6. Test restoration and emergency-input switching on an isolated station before
   each release.
7. Rotate service and source credentials without placing them in logs, command
   arguments, release artifacts, or repositories.

## Remaining certification work

Claims such as five-nines availability, SIL compliance, NATO certification, or
formal fault tolerance require independent requirements, hardware redundancy,
measured service-level objectives, failure-injection evidence, and external
audit. Software safeguards alone cannot establish those claims.

## Verification record

Verified on Windows on 2026-07-30:

- Complete suite: 789 collected, 786 passed, 3 skipped, 0 failed, 0 errors.
- Focused reliability suite: 11 passed.
- Critical playout, schema, runtime, health, and service regressions:
  88 passed plus 3 subtests.
- Windows service start/stop followed by health inspection: 3 consecutive
  clean passes with zero orphaned test services.
- Python compilation, JavaScript syntax, and patch whitespace checks passed.
