# Crossfade finalization and listener verification

## Changes

The producer finalization guard now covers `ffmpeg-transition` as well as
`ffmpeg`. Crossfade uses the same paced PCM pipe as ordinary file playout. A
clean decoder exit must keep ownership while that current pipe delivers its
buffered tail. Failed, stopped, superseded, stale, or completed pipes still
permit recovery. The existing all-output FIFO completion watermark is retained.

Rolling reload verification now requires an explicit final source acceptance on
the primary and every extra output. Optimistic socket writes and mount flags
cannot certify a successful reload. This remains source verification; decoded
listener evidence is still required separately.

## Checks

- New transition regressions failed twice before the runtime fix; all 22
  finalization cases passed afterwards, including a real held pipe and FIFO EOF.
- First broad run: 302 passed; process-isolation startup test missed its tick
  deadline. Failure evidence was retained. The isolation test passed separately.
- Complete second broad run: **303 passed**, 28 modules.
- Finalization plus source acceptance cases: **31 passed**, including nine new
  strict acceptance checks for primary and extra outputs.

## Live scope and remaining failure

Source files were backed up and copied to the live installation. Rock was
reloaded into the revised runtime; other running workers were not assumed to
load changed files automatically. Source output quality was preserved.

An existing independent listener observer was sampled for **900.031 seconds**,
2026-10-03 02:47:54–03:02:54 UTC, across 16 configured non-AI outputs. Fifteen
outputs delivered decoded PCM. `/energize` delivered zero PCM and did not
confirm source acceptance, including after one necessary Energize worker
restart. Its reconnect attempts remain enabled. Local restart did not resolve
the origin mount failure.

The observation also retained silence, decoder errors, and the intentional
Energize/Rock reloads. It is **not a zero-interruption pass**. A new Rock-low
listener decoded 12 seconds of HE-AACv2 at 64 kb/s without errors; this short
check does not erase the old listener's retained corruption evidence or certify
15 minutes of continuity.

The Windows Supervisor was RUNNING with AUTO_START. Neither a PC reboot nor an
internet outage was induced. The independent 24-hour observer remained running,
and its earlier failures were not reset. The all-mounts-open and zero-interruption
goal remains incomplete.
