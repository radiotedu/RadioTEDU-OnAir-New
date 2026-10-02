# Local cache retention, final source responses and persistent observation

## Actual observations

On 2026-10-02 UTC, a 157.817-second filesystem sample found 94.745 MiB of
visible file growth under the application data paths. Seven newly cached media
files account for almost all of it; the decoder evidence log added about 0.195
MiB. The cache contained 265 files and 3.992 GiB against its 4 GiB limit. These
are logical file measurements, not a complete attribution of physical disk
writes. A separate short sample measured approximately 1.57 MiB/s of physical
C: writes. No sustained reduction has been proven yet.

C: had 386.49 GiB free. Guest programme recording tables were empty. Heartbeat
replacement was approximately 0.043 MiB/s of logical writes in a six-second
sample, so throttling those heartbeats was not justified as the main remedy.

## Changes

* Keep the portable cache default at 4 GiB. Read a persistent local policy from
  `fast-audio-cache-policy.json` next to `FastAudioCache`, at most once per minute
  per worker. Environment budget overrides retain precedence.
* This installation uses a 64 GiB retained cache with a 32 GiB free-space reserve.
  It warms only selected media, without scanning or copying the whole library.
  The larger working set should reduce later re-copying. Initial cache misses
  still write complete media files; instantaneous write-rate reduction is not
  claimed.
* Check free space before copying. Under pressure retain the source library path
  and ask for asynchronous LRU cleanup. Include existing cache bytes when
  calculating the eviction budget, avoiding a moving budget based on free space
  alone. Recent/open cached media remains protected by existing eviction rules.
* Consume a delayed final source HTTP response after `100 Continue` or optimistic
  body startup. A final rejection raises a credential-safe error containing only
  its numeric status. Handle fragmented and coalesced response headers with a
  16 KiB bound and nonblocking housekeeping reads. Report
  `source_response_confirmed` separately from listener verification. No deadline
  was added for origins that omit their final response: a pending response is
  evidence of uncertainty, not an instruction to tear down otherwise audible
  sources.
* Decode actual PCM with transparent `silencedetect=noise=-65dB:d=0.5`
  diagnostics. Quiet programme remains valid decoded audio. Source-generated
  filler counters and listener silence measurements must be correlated; natural
  quiet passages alone do not prove a delivery failure.
* With `--restart-exited-readers`, retry exited diagnostic listeners using
  5/10/20/40/60-second bounded backoff. Only the listener processes are replaced.
  Keep the global run clock and accumulated failures. Samples after retry are
  explicitly scoped to the current reader generation; previous decoded bytes
  and restart counts are exposed separately. A reader exit makes the run fail
  even if later readers recover. `--observe-unready` enables these retries in
  the live soak wrapper; it never converts a failed observation into a pass.

## Validation and deployment

* Source transport, sink recovery, cache, monitor, live roster and decoder-error
  unit regressions: 56 passed. Pipeline builder regressions: 20 passed. Both
  selections reported five existing Pydantic/dependency warnings.
* Real local TCP origin checks passed for fragmented final `200` and `403`
  responses after the first request-body bytes; no source secret was printed.
* Source files were copied with verified SHA-256 backups before replacement:
  `H:\RadioTEDU-Backups\2026-10-02T21-49-19-095Z-retention-response`.
  Its manifest records exact before/after hashes, policy presence and worker IDs.
* Supervisor and backend were not restarted. Worker reloads are sequential and
  leave the same codecs, bitrates and output configuration in place. The
  broadcast quality contract remains eight stations / sixteen outputs.
* The diagnostic task was restarted to load the new observer implementation.
  Previous failed evidence is preserved in its original directory. The new
  run is under `Diagnostics\live-delivery-soak\20261002T215309Z`.

## Known failures: this is not a 24-hour certificate

The previous actual 15-minute observation failed. Energize primary produced no
decoded PCM, while Energize low had long quiet intervals and LoFi low repeatedly
disconnected. The subsequent real-time encoder clock correction improved sample
delivery rate on fourteen persistent listeners, but did not establish a clean
24-hour run.

After this change Energize low confirmed its final source response; primary did
not. Classic initially failed a worker settle check, but later listener snapshots
did decode its primary, FLAC and low outputs. LoFi primary still exited during
the new observation. New silence diagnostics expose several-second quiet periods
in multiple streams. These remain failures requiring investigation. Neither
local socket writes, final source acceptance, nor enabled output settings prove
continuous listener audio.

## Rollback

Restore the backed-up source files in the manifest and restore the policy file
if it previously existed; otherwise remove only that explicitly named policy
file after confirming its absolute path. Reload only affected station workers
and the diagnostic task as necessary. Do not restore an older database or delete
the media library or prior failed measurement evidence.
