# Successor PCM preparation — 3 October 2026

## Change

The scheduler still requires a clean EOF for the exact active file and the
captured PCM watermark to drain on every required encoder input FIFO before
completing a song, advertisement, or recorded programme. That ownership gate
has not been relaxed.

During that drain, a station can prepare one selected successor with the normal
FFmpeg processing chain. Up to four seconds of decoded 48 kHz stereo PCM are
held in memory. These samples are sent nowhere until the scheduler actually
starts that same source. The prepared prefix is transferred exactly once ahead
of the rest of its original decoder pipe.

Preparation is limited to local files and ordinary Icecast programme operation.
Live microphone/guest operation and local monitor output use the existing path.
Changed file size/mtime, processing filters, FFmpeg executable, source selection,
or a seek rejects a prepared decoder. Unused preparations expire after 15
seconds, and shutdown/crossfade/cancellation reaps their processes. Ownership of
background-spawned processes is protected by a lock.

The isolated scheduler checks the same FIFO completion gate during its existing
100 ms idle wait and wakes once per completed producer generation. Failures
retain their normal retry backoff. It does not run database work at that poll
rate.

Workers with no WebSocket clients skip construction of legacy UI status payloads.
Isolated workers have no clients; their authoritative state still travels in
the worker heartbeat. A worker in the API process with connected clients retains
its existing notifications. This avoids synchronous API work at a handoff and
does not change advertisement cadence, target stations, or playout priorities.

## Evidence

- 159 focused runtime, worker, advertisement, schedule, process isolation, and
  PCM preparation tests passed; five pre-existing dependency deprecation warnings.
- Three local comparisons used the installed real FFmpeg, two six-second tone
  clips, and a clocked PCM FIFO. Baseline first-successor PCM delays were
  125/125/109 ms; preparation reduced them below the host monotonic clock's
  resolution. All three pairs accepted and drained exactly 2,304,000 bytes,
  representing all twelve seconds of input audio. This is a local producer/FIFO
  experiment, not a public Icecast continuity proof.
- The baseline priority-chain integration test also failed on `d6e941f`, because
  it expected pending music ahead of an already due advertisement. Its expectation
  now follows the existing production policy and verifies that both active ads
  and active songs keep ownership until their exact EOF.
- Process isolation passed again in the complete final regression run. A prior
  combined run exceeded that test's startup wait; this was not hidden as a pass.

Local experiment artifacts are under
`C:\Users\tedu\Documents\RadioTEDU-OnAir\.tmp\diagnostics\successor-real-ffmpeg`.
Test output: `.tmp\diagnostics\successor-preparation-tests.txt`.

## Deployment and limits

Original live source files were saved under
`H:\RadioTEDU-Backups\2026-10-02T23-22-48-939Z-successor-preparation`, with hashes.
Six deployed files matched staging SHA-256 hashes. Workers are loaded separately;
the backend/Supervisor is not globally restarted. Live adoption is visible in
`runtime_status.decoder_preparation.prepared_handoff_count`.

This reduces decoder startup and scheduler waiting; it does not establish zero
silence at every transition. The independent 24-hour decoded observer remains
running with its earlier failures preserved. Energize primary was still unable
to deliver decoded audio at the time of this change, and its unconfirmed source
connection can backpressure its sibling output. A local preparation change cannot
establish that the separate origin accepted that mount. No 24-hour clean pass or
all-output success is claimed.

Rollback restores the saved source versions and reloads only affected station
workers. The new unused helper module can remain on disk. No database or saved
advertisement configuration needs to be restored.
