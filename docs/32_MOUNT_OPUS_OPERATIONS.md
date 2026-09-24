# RadioTEDU 32-mount Opus/FLAC operations

## Current protected operating mode

As of 2026-08-15, this Broadcast PC must source only the six unsuffixed mounts:
`/classic`, `/lofi`, `/radio`, `/cazz`, `/rock`, and `/energize`. Each primary
uses Opus 192 kbps. All `-low`, `-normal`, `-high`, and `-flac` outputs are
persisted disabled and must not be re-enabled until the operator explicitly
requests it. Voting, JukeLocal, SharedAI, and AI-radio services stay disabled on
this PC. This reduced mode protects the sensitive TinyIce origin from source
connection churn.

## Fixed ownership and mount count

The complete RadioTEDU system has exactly 32 public mounts.

- This OnAir PC sources 30 music mounts: six station families with primary, `-low`, `-normal`, `-high`, and `-flac` outputs.
- The Services PC owns only `/en` and `/fr`.
- `/radiotedu`, `/en-*`, and `/fr-*` must never be created by this application.

For each of `classic`, `lofi`, `radio`, `cazz`, `rock`, and `energize`:

| Mount | Codec |
|---|---|
| `/<station>-low` | Opus 64 kbps |
| `/<station>` | Opus 192 kbps in current protected mode |
| `/<station>-normal` | Opus 96 kbps |
| `/<station>-high` | Opus 192 kbps |
| `/<station>-flac` | Ogg FLAC lossless |

## Routine UI workflow

1. Open **Streaming → Quality outputs**.
2. Declare origin source capacity as at least 32; 40 is recommended. This value
   is planning input only and is not considered verified until diagnostics
   observe decoded delivery from all 30 local mounts.
3. Choose **Select approved 32-mount plan**.
4. Choose **Save and verify quality outputs**. Success requires persisted read-back.
5. Choose **Apply saved outputs now** twice to confirm.
6. Run diagnostics. It must report six Opus 96 primary mounts, libopus available, 30 local mounts, 32 system mounts, and no missing station mappings or credentials.
7. Use **Stations → Current output** to change a primary host, port, mount, protected password, or Opus preset. MP3/AAC are intentionally absent from the UI.

## Recovery behavior

- Windows owns `RadioTEDU.OnAir.Supervisor` with delayed automatic startup and service failure recovery.
- Six persisted `broadcast_autostart_enabled` flags start the music workers after a boot or service restart.
- Every mount has an independent encoder/source connector and bounded PCM queue.
- A short origin pause uses the queue reserve. A saturated or long-offline branch drops only stale audio from that branch and resumes near the current programme clock; it never blocks sibling outputs.
- Origin failure does not mark the decoder as stalled and does not cause destructive whole-station restart loops.
- The watchdog repairs genuinely stopped workers and reports public delivery separately from local programme generation.

## Maintainer edit and test map

| Change | Source | Required focused tests |
|---|---|---|
| Bitrates, suffixes, mount family | `app/services/quality_outputs.py` | `tests/unit/test_quality_outputs.py` |
| Opus/FLAC FFmpeg arguments | `app/audio/gst_pipeline.py`, `app/audio/ffmpeg_pipeline.py` | `tests/unit/test_ffmpeg_pipeline_builder.py` |
| Queue reserve, resync, reconnect | `app/audio/icecast_audio_sink.py`, `app/audio/icecast_source_transport.py` | `test_quality_backpressure_resync.py`, `test_icecast_source_transport.py`, `test_stream_continuity_monitor.py` |
| Six-station fan-out | `app/audio/station_runtime.py`, `app/engine/runtime_registry.py` | `test_multi_quality_runtime.py`, `test_playout_hardening.py` |
| Persisted settings and diagnostics | `app/api/streaming.py`, `app/api/stations.py` | quality-output, station-output, and streaming credential tests |
| Operator UI | `app/static/onair/index.html`, `app.js`, `app/static/sw.js` | all `tests/js`; bump HTML asset query and service-worker cache versions |
| Boot/watchdog recovery | `app/main.py`, `app/services/audio_watchdog.py`, Windows service/task configuration | startup autostart, watchdog, reliability, and health-wall tests |

Before source, schema, or live configuration changes, follow `docs/AI_MAINTAINER_FUNCTION_MAP.md`: preserve the dirty tree, make an online SQLite backup, copy already-modified touched files, test before restarting, and require decoded audio rather than TCP connectivity.
