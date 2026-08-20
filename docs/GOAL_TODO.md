# RadioTEDU OnAir — 10-step delivery TODO

Updated: 2026-08-12
App boundary: `RadioTEDU-OnAir-Radio` only; the older `RadioTEDU-OnAir` tree is retained solely for rollback and has no startup ownership.

## 1. Inspect and baseline

- [x] Inventory repository, services, startup tasks, processes, database, media roots, secrets references, origin, logs, voting, juke-local, AI, tests, and prior applications.
- [x] Preserve station data and secrets; migrate source credentials to machine-scope DPAPI without printing or duplicating plaintext.
- [x] Confirm authoritative station mapping: `/classic`, `/lofi`, `/radio`, `/cazz`, `/rock`, `/energize`; stale station ID 7 is not autostarted.
- [x] Confirm the approved inventory: six normal music mounts, six `-low` mounts, FLAC only for Classical/Cazz, and externally owned `/en` plus `/fr`.

## 2. Implement provisioned quality mounts

- [x] Implement the approved synchronized output set: suffix-free Opus 192 normal, Opus 32 `-low` for all six music stations, and FLAC only for Classical/Cazz.
- [x] Preserve all unsuffixed legacy mounts without aliasing, redirecting, renaming, or re-encoding.
- [x] Suppress public title/artist metadata; keep one internal compliance play with delivered variants instead of counting four broadcasts.
- [x] Enable all 8 approved quality variants so all 14 local sources keep encoding and retrying while TinyIce is disconnected; retain the two external mounts in the 16-mount system plan.

## 3. Finish operator-controlled functions

- [x] Add in-app quality settings, validation, save/read-back, diagnostics, and safe apply for the six provisioned quality stations.
- [x] Keep `/en` and `/fr` as single automatic AI streams; do not invent quality URLs absent from TinyIce.
- [x] Keep voting genre-only and royalty-free; do not expose or vote on individual songs.
- [x] Let juke-local select every technically playable local song, independent of royalty metadata.
- [x] Make the streaming-management form deterministic, remove a malformed dynamic show route, and verify all 74 operator-wall JavaScript contracts.
- [x] Repair compliance variant accounting additively; preserve all 3,088 historical rows and verify both the source database and H: backup with SQLite integrity checks.
- [ ] Finish authenticated live UI click-through after origin recovery.

## 4. Harden continuity and recovery

- [x] Install the OnAir LocalSystem supervisor as immediate automatic with bounded recovery; auxiliary agents may remain delayed-auto.
- [x] Add paced source writes, bounded quality queues, stale-audio drop/resync, last-valid AI playlist startup, and station isolation.
- [x] Use immutable content-addressed AI playlists, BOM-tolerant config reads, concurrent atomic status writes with bounded Windows sharing retries, and background catalog refresh.
- [x] Index the mature AI announcement cache off the one-second station scheduler; prevent the 6,945-file metadata scan from blocking track advance/recovery and verify bounded scheduler/PCM ages on all six installed timelines.
- [x] Remove source credentials and destination URLs from FFmpeg process command lines; perform TinyIce/Icecast authentication only in a protected in-memory socket transport that retries forever with bounded reconnect backoff.
- [x] Install a five-minute two-pass watchdog with repair cooldown, `StartWhenAvailable`, and an explicit SYSTEM startup trigger so monitoring resumes after every Windows boot.
- [x] Update the managed FFmpeg/FFprobe binaries to the checksum-verified 9.0.1 Windows release and verify 192 kbps `libopus` encoding before deployment.
- [x] Add a fail-closed `tools/Repair-TinyIce-Origin.ps1` that restarts only a unique TinyIce service/task and verifies HTTP; it never kills an unowned process or reboots the host.
- [ ] Run/install that recovery on `10.98.98.75`; current workstation identity lacks remote service-control authorization.

## 5. One-shot repair/install/start

- [x] Create idempotent `tools/Install-RadioTEDU-OneShot.ps1` with backup, DPAPI migration, ACLs, service definitions, recovery, watchdog, and old-startup removal.
- [x] Require at least 16 source slots and recommend 20 for the 16-mount plan; keep every enabled source retrying independently.
- [x] Run one-shot locally in protected legacy-only mode.
- [x] Require real OnAir readiness and an AI supervisor child, not only a running Windows service wrapper; write generated JSON without a BOM.

## 6. Install and migrate

- [x] Install OnAir, AI, Shared AI, Voting, and Juke services without deleting data, secrets, playlists, ledgers, or rollback programs.
- [x] Remove the old unrelated OnAir shortcut/task from startup and archive the shortcut for rollback.
- [x] Keep durable database/config backups on `H:` with integrity/hash verification.

## 7. Controlled cold start

- [x] Verify all five local services are automatic/running, OnAir `1.0.2` is ready, six station workers produce fresh non-stalled PCM, both AI branches stream, and the watchdog task is ready.
- [ ] Restart TinyIce, start legacy sources first, and prove reboot recovery without manual/Codex intervention.

## 8. Verify every stream and function

- [ ] Probe all eight legacy mounts for connection, continuous decode, audibility, and metadata suppression.
- [ ] When TinyIce recovers, verify all 8 additional music mounts for codec, bitrate/lossless profile, continuity, sync, and independent failure recovery.
- [ ] Verify AI, voting, juke-local, mobile fallback, compliance exports, settings, and watchdog through production paths.

## 9. Monitored soak and fault injection

- [ ] Run controlled source/network/encoder/service failures and prove bounded station-scoped recovery with no legacy regression.
- [ ] Run two real 600-second watchdog cycles and an unattended audible soak; fix every drop, disconnect, dead-air, drift, or restart defect found.
- [x] Accept the clean local 600-second rerun only if all six timelines have zero failures. Final exact-source result after the cache refresh-race repair: 600.031 seconds, 462 samples across all six stations, zero failures, worst heartbeat age 3.476 seconds, worst delivered-PCM age 0.344 seconds. The earlier failed runs and the first zero-failure run remain on `H:` as diagnostic evidence.

## 10. Versioned handoff

- [ ] Only after the soak passes, finalize exact-provenance version 1.0.2 packaging/installer artifacts.
- [ ] Deliver operator runbook, architecture/threat/failure analysis, backup/restore and rollback procedures, acceptance evidence, and verified mount inventory.
- [ ] Keep previous programs until every migrated function/configuration is verified stable.

## Current blocker and automatic recovery state

The remote TinyIce origin at `10.98.98.75:11154` accepts TCP but returns no HTTP bytes even with every RadioTEDU source stopped. Nginx responds, but its stream request hangs on the origin. Local OnAir and AI supervisors are running and will reconnect automatically when TinyIce is restarted. The current Windows identity has no remote service-control credential, so origin restart authorization is still required.

Latest direct isolation: TinyIce accepts the authenticated source handshakes and consumes encoded data, but listener GETs for all eight legacy mounts time out with zero HTTP/audio bytes. Nginx port 80 returns its normal 301 and its streaming/TLS path cannot obtain a usable origin response. The installed watchdog reproduced this twice, exited with the explicit `origin_unavailable` result `20`, and did not restart healthy local sources or AI.

Local soak diagnosis: the first 600-second timeline run failed with 44 observations, including a 26.843-second PCM-age peak on station 4. AI-enabled workers were reparsing 6,945 cache metadata files every scheduler tick. A shared non-blocking dedupe-key index now builds large caches on a daemon thread, refreshes without invalidating the last index, coalesces changes observed during a scan into one follow-up refresh, and receives newly generated announcements immediately. Follow-up runs reduced this to short 2.25–2.828-second decoder handoffs; the output encoder was already receiving continuity PCM, but the delivered-output health clock ignored those writes. Continuity PCM now advances output health, preventing false stall recovery churn while preserving the strict decoder and scheduler diagnostics. The final exact-source local run passed: 600.031 seconds, 462 samples, six stations, zero failures, worst delivered-PCM age 0.344 seconds. Public audible soak remains gated by TinyIce recovery.
