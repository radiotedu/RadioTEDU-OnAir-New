# Worker I/O optimization

## Runtime changes

Routine scheduler and independent liveness file snapshots share one write per
second. Boot, shutdown, programme phase/source, error, runtime availability,
and output acceptance changes bypass the interval. The publisher serializes
both threads. A failed write remains immediately retryable. RPC health reporting
and the independent scheduler-stall calculation remain available.

Each child now keeps one StationWorker/SQLite connection across successful
ticks. Tick failures close it and the next tick constructs a fresh worker.
Unfinished transactions are rolled back at the existing tick boundary, matching
the previous close-per-tick behavior. Shutdown closes the retained connection.
Committed ownership and advertisement records keep their FULL/WAL durability.

## Checks and deployment

- **334 passed**, 31 modules, including durable playout/ad transitions, source
  transport, FIFO completion, process isolation/restart, and the new helpers.
- New helpers cover connection reuse, failure renewal, transaction rollback,
  committed state after restart, simultaneous heartbeat writers, immediate
  faults/phases, stopped/ready snapshots, and write retries.
- Three files were backed up and installed; dependencies were installed before
  the child entry point. All eight configured non-AI workers were reloaded
  sequentially while the separate broadcast endpoint was confirmed refusing
  connections. Each replacement PID, fresh heartbeat, and scheduler tick was
  observed. This did not certify listener delivery.

## Measured file writes

Two sequential live 60-second windows during the same origin outage:

| Metric, all eight workers | Before | After |
| --- | ---: | ---: |
| Observed heartbeat file changes | 1,383 | 464 |
| Sum of changed-file payload sizes | 7,900,131 bytes | 2,731,990 bytes |
| Longest observed change interval | 1,354 ms | 1,368 ms |
| Missing file reads | 0 | 0 |

File replacements fell **66.45%**, observed payload volume **65.42%**. These
numbers do not measure total PC writes or physical SSD write amplification,
filesystem journals, WAL, or normal connected audio performance. The stable
fake-clock case separately verified 600 routine ticks plus 60 liveness calls
produce 60 writes while state changes remain immediate.

The existing audio cache retains unchanged media for bounded reuse; it is not
deleted after every playout. Its live policy is a 64 GiB ceiling and 32 GiB
minimum free space. Audio PCM reserve remains in memory. This update did not
change audio codec, bitrate, mount configuration, or programme/ad settings.

## Incomplete objective

The origin `stream.radiotedu.com` resolves to a separate host, 10.98.98.75.
Port 11154 continues to return TCP ECONNREFUSED while the local application
responds. All workers remain running and retrying. Origin access was requested
from the user. The existing independent 24-hour observer remains alive with
earlier failures retained; all mounts open and 24-hour zero interruptions have
not been verified. Normal delivery must be checked after origin restoration.
