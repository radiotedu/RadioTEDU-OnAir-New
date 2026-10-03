# Keep queue-owned audio while its decoded tail drains

## Observed failure

A read-only live sampler recorded Radio's TEDU_2 sweeper starting at 00:58:24 UTC,
then restarting at 00:58:32 and again at 00:58:39 with the same active filename.
The decoder had exited cleanly while roughly six seconds of programme PCM
remained in both output FIFOs. Sink writers and network writes were healthy.

The music/manual queue checked `program_running` and metadata/startup time before
checking `producer_draining`. A fast-decoded source can finish ahead of its
catalog time, so the queue treated that retained tail as an unexpected stop and
retried the same item. This introduced repeated sweepers, redundant decoding and
additional database transitions. The ad path already checked matching drain
before its elapsed-time recovery logic.

## Fix

After checking fully drained clean EOF, the queue now returns without advancing
or restarting when the runtime proves that **this exact queue-owned URI** is
still draining. This check precedes startup recovery, unknown-duration handling
and metadata safety timers. A different URI or failed decoder still recovers;
only fully drained EOF completes the row. The decoder's all-output acceptance
and drain watermark rules remain unchanged.

## Verification

Nine focused checks cover short sweepers, music before/after catalog duration,
unknown duration, announcements, long programmes, wrong-file drain, failed
decoder recovery and clean EOF. Before the fix, the six matching-tail cases
failed; afterward all nine passed. The full focused playout suite passed 204
checks with five existing dependency warnings.

The source was backed up under
`H:\RadioTEDU-Backups\2026-10-03T01-04-50-341Z-queue-drain-guard` before deployment.
The live source is verified by SHA-256 and workers are reloaded individually.

## Remaining work

This prevents an observed scheduler restart bug; it does not certify a clean
24-hour run. The independent listener observer retains all existing failures.
Rock also showed primary FIFO saturation and branch recovery during the capture,
with low-output filler while the primary recovered. Energize primary remains
unconfirmed and supplies no decoded listener PCM. Those transport/recovery
failures require separate investigation; no output quality was reduced.
