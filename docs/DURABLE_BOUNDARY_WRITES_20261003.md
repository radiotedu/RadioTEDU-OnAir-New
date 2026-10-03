# Durable playout writes and lease renewal — 3 October 2026

## Changes

- Related queue completion, track statistics, immutable music-use recording and
  playout ownership updates now share one SQLite transaction. Ad completion also
  groups its ownership and track updates. Starting the next managed item commits
  both ownership records together **before** calling the audio runtime.
- SQLite journal and synchronous settings are unchanged. The deployed database
  continues to use WAL with FULL synchronization. An inner rollback aborts the
  group even if a best-effort caller catches the original exception. CSV export
  and UI notifications happen after the actual commit.
- A worker reads the persisted lease owner on every scheduler tick. A valid
  lease is durably renewed after one third of its lifetime instead of every tick.
  Acquisition still uses a conditional UPSERT. Invalid, expired and unreasonable
  future leases are reclaimed; an unexpected owner blocks the worker immediately.
- EOF, output drain watermarks, ad priority, retry persistence and output quality
  are unchanged.

## Evidence

195 focused unit/integration checks passed, with five existing dependency warnings.
Coverage includes atomic rollback, a failed final commit, visibility from another
connection before PCM starts, deferred notifications, clean ad EOF, concurrent
lease acquisition, changed ownership and clock correction. The music-use schema
test had a stale literal version 20; it now checks the current schema version while
retaining all reporting-table assertions.

A read-only 60-second production lease sample compared newly loaded Rock with the
older workers. Rock renewed six times, approximately every 10.09 seconds. The
older regular workers renewed 163–173 times over that window. This measures lease
write frequency, **not** total physical SSD traffic or a system-wide percentage.

Rock's first natural music transition after deployment added zero continuity
silence chunks on both /rock and /rock-low. Longer listener verification remains
necessary; one clean transition does not establish all-day continuity.

Subsequent transitions in the same 15-minute sampler **did** add silence: Rock
accumulated at least 304 primary and 139 low-output filler chunks, then both
sink counters reset without a worker PID change. The analysis retains positive
increments across those resets. Newly loaded Radio accumulated at least 262
primary and 107 low-output chunks over its sampled song/sweeper/song boundary.
The disk-write reduction therefore does not by itself solve every handoff gap.

An earlier isolated EOF profile showed four separate queue-completion commits and
two start-ownership commits. They are now one commit per group. A subsequent
private-snapshot profile also encountered a 47-second first lease commit after a
large snapshot copy/checkpoint. That outlier makes its timing unsuitable as a
controlled before/after performance result. Deterministic transaction tests and
the live renewal sample provide the narrower evidence above.

## Deployment and remaining failures

Four source files were backed up under
`H:\RadioTEDU-Backups\2026-10-03T00-30-02-871Z-durable-boundary-writes`.
The helper, usage service, lease service and station worker were copied to the
live Radio checkout at 00:31:25 UTC and verified by SHA-256. Workers are reloaded
individually; the backend and Supervisor remain running.

All eight station workers completed one rolling reload for these changes. The
short reload verifier observed advancing local send counters on all outputs;
that observation is not a listener-delivery pass, particularly for Energize.

The independent 24-hour listener observer remains active, with its previous
failures preserved. The /energize origin listener returns HTTP 200 and AAC
metadata but supplied no audio bytes during a 12-second probe. /energize-low and
/radio supplied bytes. Energize's primary source is still unconfirmed and the low
output has recorded quiet intervals. This release does **not** claim that all 16
outputs are healthy or that a zero-interruption 24-hour run has passed.
