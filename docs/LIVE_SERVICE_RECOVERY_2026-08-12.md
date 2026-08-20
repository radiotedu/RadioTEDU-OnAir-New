# Live service recovery evidence — 2026-08-12

## Result

RadioTEDU OnAir 1.0.2, AI English/French, local genre voting, juke-local, and the shared local AI engine are running locally. The remote TinyIce origin later became globally unresponsive, so no public mount is currently claimed live. The one-shot service/startup repair is installed; no final release package was built.

The one-shot was reapplied in protected legacy-only mode at `2026-08-12T14:10:48Z`. Its durable machine-readable result is `C:\ProgramData\RadioTEDU\OnAir\one-shot-install-state.json`: OnAir running, AI running, watchdog ready, old startup removed, and quality outputs disabled.

## Verified runtime

- OnAir API: live, version `1.0.2`.
- Configured legacy public mounts: `/classic`, `/lofi`, `/cazz`, `/energize`, `/radio`, `/rock`, `/en`, `/fr`; listener verification is blocked by the hung TinyIce origin.
- Voting: automatic Windows service `RadioTEDUVotingRadio`; `genre` mode; `operator-approved-royalty-free` selection policy; local MP3 stream playing on port 4320.
- Juke-local: automatic Windows service; health `ok`; 35,813 technically playable files; `all_playable`; rights filter disabled as explicitly required for per-song requests.
- Shared AI: automatic Windows service; Ollama API online with one installed model.

## Reliability changes

- AI source credentials remain referenced through the existing protected credential store and are not written into commands or logs.
- AI startup reuses a validated ffconcat cache and refreshes the catalog after audio is already online.
- AI playlists are immutable/content-addressed, and EN/FR status updates use unique atomic files plus bounded Windows sharing retries; health-file contention cannot terminate playback.
- AI announcement lookup uses a shared non-blocking dedupe-key index. A mature 6,945-file cache is never parsed inside a station's one-second scheduler tick, and refreshes preserve the last usable index until the replacement is ready.
- An invalid cached playlist is discarded when it cannot produce initial audio.
- FFmpeg pipe reads use available-data reads instead of waiting for a full 16 KiB block.
- A CBR output pacer splits encoder bursts into 1 KiB source packets and prevents catch-up bursts after a stall.
- Music output authentication was removed from FFmpeg command lines. The encoder now writes only encoded bytes to a pipe; a protected in-memory transport performs the authenticated source handshake and bounded reconnect, so local process inspection cannot reveal host/user/password source URLs.
- The watchdog probes all eight public mounts, confirms global origin responsiveness twice, rate-limits repairs, and suppresses every healthy local/AI restart when TinyIce itself is hung.
- The scheduled task is `IgnoreNew` and `Highest`, so only one authorized recovery instance runs.

## Verification evidence

- AI supervisor tests: 15 passed; installed restart verified one child, EN 140 tracks, FR 212 tracks, both streaming with no refresh/status error.
- One-shot/installer contract tests: 22 passed; the installed one-shot then verified OnAir readiness and the real AI child.
- Operator-wall JavaScript tests: 74 passed.
- Compliance migration: 3,088 historical plays preserved; additive delivered-quality accounting column installed; source and H: backup both passed `PRAGMA integrity_check`.
- Controlled OnAir restart: all six music workers returned with fresh PCM, no PCM stall, and zero initial dropped chunks; listener health remained false only because the origin was not consuming/responding.
- Protected transport regression gate: 101 tests plus 3 subtests passed; installed process inspection found zero FFmpeg commands containing the origin host, port, Icecast URL, username, or password.
- Voting agent tests: 44 passed across 11 test files.
- Voting production build: TypeScript check and Vite build passed.
- Voting local stream: 15-second decode, exit 0, no error lines, approximately real-time wall duration.
- AI controlled restart: both mounts returned in 4–6 seconds from cached playlists.
- AI listener decode after pacing: `/en` and `/fr` both exit 0 with no decode errors.
- Earlier scheduled watchdog evidence showed all eight mounts audible. The latest installed-guard run returned task result 20 with `origin unavailable`; it correctly suppressed all local and AI restarts.
- The first local 600-second timeline soak failed and is retained on `H:`: 44 observations, with station 4 reaching a 26.843-second program-PCM age. The cause was the per-tick full AI cache scan. After replacing it, follow-up runs exposed only short decoder boundaries where the encoder was already receiving continuity PCM. The delivered-output health clock now accounts for that PCM, avoiding false output-stall repairs while retaining separate decoder-state diagnostics. All six installed timelines have fresh output PCM, zero worker failures, and scheduler ages within the acceptance threshold before the exact-code rerun.
- Exact-code local acceptance soak passed: 600.094 seconds, 428 samples over all six music stations, zero failures, worst heartbeat age 4.363 seconds, and worst delivered-PCM age 0.5 seconds. Evidence: `H:\RadioTEDU-OnAir-System-Backup\recovery\2026-08-12\local-playout-600s-final2.json`.
- Final exact-source acceptance after cache refresh-race hardening also passed: 600.031 seconds, 462 samples, zero failures, worst heartbeat age 3.476 seconds, and worst delivered-PCM age 0.344 seconds. Evidence: `H:\RadioTEDU-OnAir-System-Backup\recovery\2026-08-12\local-playout-600s-exact-final.json`.
- Final focused regression gate: 72 Python continuity/cache/AI/process-isolation/quality/origin-repair tests passed. Operator-wall gate: 70 JavaScript tests passed. Post-install readiness probe: 20/20 succeeded, maximum 1.688 seconds and average 1.059 seconds.
- Post-install functional checks: OnAir 1.0.2 ready; EN streaming with 140 tracks; FR streaming with 212 tracks; local voting API exposes three genre-only candidates and its MP3 listener returned 4,096 bytes; juke-local reports 35,813 `all_playable` files with the rights filter disabled; SQLite integrity is `ok`; no FFmpeg command exposes an Icecast destination or source credential.

## Open gates

1. The deployed website backend responds at `/jukebox/health` but returns 404 for both public next-song-voting route forms. Public synchronization is therefore disabled while local genre voting continues. Deploy the repository's existing route and re-enable synchronization only after authenticated publication is verified.
2. The active main station catalogs do not contain sufficient rights evidence to claim that every track is royalty-free. The catalog includes recognizable commercial collections; only 24 active rows across the six stations have a non-empty rights reference. Streaming was preserved, but the royalty-free claim remains unverified until an approved inventory and supporting references are supplied.
3. Superseded by the current 16-mount contract: the broadcast PC keeps 14 music sources active—six suffix-free Opus 192 normal streams, six Opus 32 low streams, and FLAC only for Classical/Cazz. Rejected branches retry independently without stopping playout. `/en` and `/fr` remain externally owned.
4. TinyIce at `10.98.98.75:11154` accepts the authenticated source handshakes and consumes encoded data, but every legacy listener request times out without HTTP/audio bytes. Remote service-control authorization is required to restart it; local supervisors reconnect automatically and no longer churn healthy programme timelines.
