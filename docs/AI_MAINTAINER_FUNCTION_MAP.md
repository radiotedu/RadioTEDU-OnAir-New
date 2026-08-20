# RadioTEDU OnAir — operator and AI maintainer function map

This is the canonical change map for RadioTEDU OnAir. It is written so an operator can run the station from the UI and a future AI maintainer can find the smallest correct source, test, and deployment path without rediscovering the system.

## Non-negotiable production invariants

- Current protected mode sources exactly six mounts from this PC: `/classic`, `/lofi`, `/radio`, `/cazz`, `/rock`, and `/energize`, all at Opus 192 kbps.
- All `-low`, `-normal`, `-high`, and `-flac` quality outputs are persisted disabled until the operator explicitly requests re-enabling them. Do not restore the 30-source plan automatically.
- `/radiotedu` is not a real mount and must remain absent.
- `/en` and `/fr` belong to the independent AI-radio computer and are not sourced by this OnAir instance.
- Never synthesize `/en-*`, `/fr-*`, `/radiotedu`, or `/radiotedu1`.
- The dormant quality plan remains Low 64 kbps, Normal 96 kbps, High 192 kbps, and FLAC only on `-flac`; it is capability documentation, not the active source plan.
- MP3 and AAC remain readable only for historical configuration compatibility. They are not operator presets, defaults, automatic rollback targets, or production fallbacks.
- TinyIce is sensitive: source-write health is authoritative. Do not run recurring listener probes, and never disconnect a healthy source because a listener GET failed.
- Never put credentials in source, documentation, browser storage, diagnostics, or chat. Protected `.env` files and the credential vault are the authority.
- A UI mutation is complete only after persisted read-back matches the requested value.
- Never permanently delete operator media from the UI. JukeLocal retirement is a reversible move into `.radiotedu-trash`.
- Do not clear or advance the current queue when stopping a broadcast.

## Deterministic operating model

For identical persisted database state, RadioTEDU selects the same next item:

1. Pending queue rows are ordered explicitly.
2. Library fallback first minimizes play count and last-played time.
3. A saved per-station `autoplay_shuffle_seed` provides stable hash tie-breaking.
4. Jingles use least-played, last-played, then track-ID order.
5. JukeLocal listings use configured-root order followed by case-insensitive relative path and exact relative path.
6. Every UI save reads the saved state back before reporting success.

The operator changes the rotation key in **On Air → Deterministic rotation key**. The default is `radiotedu-onair-station-<station_id>`.

## Function-by-function edit map

| Function | Operator UI | Backend/API authority | Core implementation | Primary tests | Safe edit process |
|---|---|---|---|---|---|
| Start, resume, stop, skip | On Air and Queue | `app/api/runtime.py` | `app/engine/runtime_registry.py`, `app/engine/station_worker.py`, `app/audio/station_runtime.py` | `test_operator_broadcast_control.py`, `test_runtime_registry.py`, `test_station_runtime.py` | Preserve the current queue item on stop; serialize mutations per station; verify runtime plus queue read-back. |
| Automatic boot/recovery | On Air restart checkbox; Diagnostics watchdog | `app/api/runtime.py`, `app/api/watchdog.py` | `app/main.py`, `app/services/audio_watchdog.py`, service-host configuration | `test_startup_runtime_autostart.py`, `test_reliability.py`, `test_playout_hardening.py` | Persist `broadcast_autostart_enabled`; start all six music workers after delayed service startup; let each mount reconnect independently; never restart a whole station merely because the origin is offline. |
| Deterministic rotation | On Air rotation key | `/api/settings/station` in `app/api/legacy.py` | `app/engine/station_worker.py`, `app/engine/broadcast_queue_autofill.py` | `test_station_worker_fallback.py`, `test_juke_library_admin.py` | Validate 3–120 printable characters, persist `playback_selection_policy=stable_rotation`, then read back both fields. |
| Station creation/deletion | Stations | station endpoints in `app/api/legacy.py` | station repositories and `app/db.py` | `test_station_output_api.py`, operator wall tests | Keep station IDs stable; prevent deletion while runtime owns resources; never synthesize `/radiotedu`. |
| Mount, host, codec, output | Stations → Current output; Streaming → Quality outputs | `app/api/stations.py`, `app/api/stream_config.py`, `app/api/streaming.py` | `app/services/stream_config_service.py`, `app/services/quality_outputs.py`, `app/repositories/station_output_repo.py` | `test_station_output_api.py`, `test_quality_outputs.py`, `quality_outputs_panel.test.cjs`, `test_ffmpeg_pipeline_builder.py` | Production uses six unsuffixed Opus 192 primaries, six Opus 32 `-low` branches, and FLAC only for Classical/Cazz. Always require persisted read-back. |
| Encoder and source continuity | Diagnostics / Streaming health | `app/api/streaming.py` | `app/audio/icecast_source_transport.py`, `app/audio/icecast_audio_sink.py`, `app/audio/ffmpeg_pipeline.py` | `test_icecast_source_transport.py`, `test_stream_continuity_monitor.py`, `test_quality_backpressure_resync.py`, `test_playout_hardening.py` | Current protected mode has no application write timeout on an established source socket. Preserve it through TinyIce backpressure; reconnect only after the peer/network closes it. Keep bounded PCM queues, silence continuity, and staggered reconnects. |
| Station music library | Media → Managed station library | library routes in `app/api/legacy.py`, `app/api/library_automation.py` | `app/services/managed_library_watcher.py` and track repository | library autoplay/import tests, `test_managed_library_watcher.py` | Choose folder in UI, persist recursive/skip/mode settings, then verify active-file count and watcher status. Stable changes hot-import; transient sync errors retry forever with a five-minute cap. |
| Jingles | Automation | jingle/sweeper routes in `app/api/legacy.py` | `app/engine/broadcast_queue_autofill.py`, station worker | sweeper and legacy runtime tests | Upload or select a jingle folder in UI. Treat legacy wire value `random` as stable shuffled rotation; do not introduce process randomness. |
| Startup sound | Automation | startup-sound routes in `app/api/legacy.py` | station worker startup-sound functions | operator wall and runtime tests | Use fixed track or deterministic approved-jingle rotation. Upload, save, and verify the returned track ID and policy. |
| JukeLocal songs | Services → JukeLocal song library | Juke library routes in `app/api/integrations.py` | `app/services/juke_library_admin.py` | `test_juke_library_admin.py`, `test_unified_operator_sections.py` | Resolve only roots from the protected Juke `.env`; reject traversal/symlinks/duplicates; upload atomically; retire into protected trash; restore through the UI. Never expose the request secret. |
| JukeLocal process | Services → Juke Local Media Agent | service-control routes in `app/api/integrations.py` | `app/services/radiotedu_service_control.py` | `test_radiotedu_service_control.py`, commissioning tests | Save source/config/health paths first. Windows SCM owns autonomous startup. Check health before start/restart and use two-click confirmations. |
| Voting | Services → adapters and managed services | `app/api/integrations.py` | voting agent/backend definitions in service control | voting and service-control tests | Keep optional voting failure isolated from core playout. Store tokens only through the credential vault/UI password field. |
| AI host for normal stations | Services → AI host | AI routes and `app/api/setup.py` | `app/services/ai_host.py`, `app/services/ai_prefetch.py` | AI readiness and setup tests | AI may warm or fail without stopping music. Save settings, test voice, then verify readiness. |
| Independent EN/FR AI radio | Services → EN + FR supervisor | managed-service API only | external `C:\RadioTEDU` service | service-control and provisioning tests | This PC may monitor/control the configured service, but OnAir must not source `/en` or `/fr`. |
| Service paths and lifecycle | Services | `/api/integrations/radiotedu/services*` | `app/services/radiotedu_service_control.py` | `test_radiotedu_service_control.py`, `rtai_deterministic_wall.test.cjs` | Commands are fixed allowlisted definitions. UI edits paths/settings; credentials remain protected. Read health after each action. |
| Scheduling/dayparts | Scheduler and Dayparting | schedule/daypart APIs | scheduler and station worker | schedule/daypart tests | Require complete timezone-aware coverage and explicit queue ordering. Avoid current-time randomness in selection. |
| Shows/guests/live input | Shows and On Air | show, studio, guest-room, audio APIs | live-input and show services | show/permission tests | Preserve ownership and permission checks. Stop/restore transitions must be explicit and reversible. |
| Advertising | Advertising | ad and campaign APIs | ad repositories/runtime | advertising tests | Keep break clocks deterministic and do not interrupt emergency content. Verify queue/read-back after mutations. |
| Compliance/music ledger | Compliance | `app/api/music_usage.py` | `app/services/music_usage.py`, `app/services/juke_music_usage.py`, `tools/export_official_music_usage.py` | `test_music_usage.py`, `test_juke_music_usage.py` | Ledgers are append-only/hash-chained. `RadioTEDU Official Music Usage Export` writes atomic current, daily, and month-close reports to H: and verifies both historical hash versions. Never rewrite ledger rows. |
| Backup/recovery | Backup / Recovery | recovery APIs | `tools/backup_onair_state.py`, reliability helpers | recovery and installer tests | Before schema/config work, make an SQLite online backup and run `PRAGMA integrity_check`. Restore only through an explicit confirmed workflow. |
| Health wall/watchdog | Diagnostics | `app/api/health.py`, `app/api/health_wall.py`, watchdog API | watchdog service and tool | health-wall/watchdog tests | Report local playout separately from public-mount delivery. Do not call a TCP-open mount “live” until audio bytes decode. |
| Quality outputs | Streaming | `app/api/streaming.py` | `app/services/quality_outputs.py`, station runtime extra outputs | quality-output and multi-quality runtime tests | Keep every quality variant disabled in current protected mode. Re-enable only after an explicit operator request, then save, verify read-back, and apply. `/en` and `/fr` remain external. |
| UI/service-worker shell | All views | static routes in `app/main.py` | `app/static/onair/index.html`, `app.js`, `styles.css`, `app/static/sw.js` | all `tests/js`, deterministic wall contract | Every new form/button needs an explicit handler and unique ID. Bump HTML asset query versions and service-worker cache name after UI changes. |

## Standard AI change procedure

1. Read this file and the closest feature-specific runbook.
2. Inspect `git status`; preserve all unrelated operator/agent work.
3. Back up the live SQLite database with the SQLite online backup API and verify `PRAGMA integrity_check = ok`.
4. Copy each already-modified source file that will be edited into a timestamped backup folder.
5. Change the source checkout at `C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio`; do not hand-edit installed caches or database rows unless performing a documented migration.
6. Add or update a test proving persistence, read-back, permissions, deterministic ordering, and rollback behavior.
7. Run Python compilation, JavaScript syntax checking, focused Python tests, and all JavaScript tests.
8. Bump static asset versions when HTML, CSS, or JavaScript changes.
9. Restart `RadioTEDU.OnAir.Supervisor` only after tests pass. Confirm delayed automatic startup remains enabled.
10. Verify the UI from `http://127.0.0.1:18110/`, then verify saved values through API read-back.
11. For live audio, canary one station family first, then verify all 14 local mounts sequentially or with bounded concurrency. A successful TCP connection is insufficient; require decoded audio continuity and the expected codec.
12. Verify `/en` and `/fr` only as external listener streams; never create their source connections here.
13. If TinyIce accepts TCP but returns no bytes and remote administration is denied, stop repeated probing and follow `docs/TINYICE_ORIGIN_RECOVERY_RUNBOOK.md`.

## UI authority checklist

The operator should not need Codex for routine work:

- Start/stop/resume stations and choose restart behavior.
- Change the deterministic rotation key.
- Change normal output host, mount, protocol, codec profile, and credentials.
- Select, save, apply, diagnose, and disable/re-enable the approved 16-mount Opus/FLAC plan.
- Import station music; select managed station and jingle folders.
- Upload/configure jingles and startup audio.
- Upload, search, safely retire, and restore JukeLocal songs.
- Configure and check Voting, Juke, Ollama, and AI services.
- Start/stop/restart managed services where the service ownership model permits it.
- Configure schedules, dayparts, shows, guests, ads, compliance metadata, and recovery points.
- Diagnose local runtime, watchdog, public-mount delivery, and service health.

Code changes, schema migrations, origin-server administration, and first-time Windows service commissioning remain maintainer tasks; routine broadcasting and content operations are UI tasks.
