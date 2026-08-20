# RadioTEDU OnAir 1.0.2 Verification Report (separate RadioTEDU build)

Date: 2026-08-10
Platform: Windows 10 22H2 (10.0.19045), x64
Python: 3.12
.NET SDK: repo-local 8.0.415
Installer compiler: Inno Setup 6.7.3

## Automated verification

- Current full Python/integration collection: 1,026 passed, 3 expected
  Playwright-plugin skips, and 3 subtests passed in 24:03. This includes the
  large-library incremental sync, redacted encoder diagnostics, strict soak
  stability gates, ACL-owner fallback, offline recovery staging, and README
  parity corrections. The skipped visible-browser cases were separately
  exercised in the documented foreground Edge run.
- Focused confirmation also passed: managed library/API/UI contracts
  `23 passed`; Icecast/recovery diagnostics `21 passed`; strict soak
  projection `3 passed`.
- Final packaging regression slice: `31 passed`; this verifies the deterministic
  backend staging contract and the managed `tools\bin` FFmpeg/ffprobe copies
  required by the installer. FFplay is now sourced and hashed from the same
  pinned FFmpeg distribution. The slice also verifies that running repository-local
  workers are stopped before canonical cleanup and that a timestamp fallback is
  promoted as a complete bundle rather than copying only its executable.
- Recovery/backup/ACL slice: `19 passed`; authenticated access-control and
  recovery slice: `28 passed`; full supervisor suite: `54 passed`; JavaScript
  UI suite: `70 passed`; installer handoff static scripts: all passed.
- JavaScript UI/PWA contracts: 70 passed, 0 failed.
- Windows desktop tests: 54 passed, 0 failed.
- Packaged desktop bundle smoke: passed on an automatically allocated,
  isolated loopback port. The packaged backend, agent, shell, and installer
  marker were all resolved.
- Final targeted runtime, health, public-status, identity, metadata, and
  operator-authorization regression slices passed after their respective
  fixes.

## Visible mouse and keyboard verification

The final operator workflow was exercised in a visible Microsoft Edge window.
Functional state changes were made with real mouse and keyboard events. API
reads were used only to confirm state after the visible interaction.

- RadioTEDU OnAir branding and RadioTEDU logo
  rendered correctly.
- The controlling station was RadioTEDU Lo-Fi (`/lofi`).
- Broadcast Stop required two mouse clicks, stopped the scheduler, engine, and
  output, and preserved all 19 queued items in order.
- Broadcast Start was clicked with the mouse after a backend restart and
  verified scheduler, engine, and output feed recovery.
- Final visible state: `ON AIR`, engine running, scheduler running, Icecast
  connected, AI disabled, emergency source off, restart policy disabled.
- Read-back verification showed station 2 (`/lofi`) running with its worker,
  program, feed, and Icecast sink active. Stations 1, 4, 5, 7, 8, and 9 were
  all stopped.
- The only established encoder connection was the `/lofi` Icecast output.
- Stop/resume, station selection, output save/test, station create/delete,
  queue reload/reorder/remove, library search/filter/pagination, folder
  selection/cancel, AI enable/disable, integrations, self-check, repair,
  password mismatch handling, activity clearing, and shared-brand settings
  were exercised from the UI.
- Microphone authorization, live input, music modes, and console controls were
  exercised from the full control surface.
- Emergency takeover opened a browser audio source, verified captured audio
  frames on `/lofi`, then stopped and restored scheduled playout.
- Automatic jingles were disabled and re-enabled, changed from every 2 songs
  to every 3 songs and back to 2, and switched between ordered and random
  selection. Final setting: enabled, every 2 completed songs, random.

Rendered evidence:

- `docs/assets/onair-console-verified.png`
- `docs/assets/jingle-control-verified.png`

The Codex in-app browser connector failed during its own initialization. The
same visible workflow was completed by attaching Playwright to a normal,
foreground Microsoft Edge window and sending mouse/keyboard input to it.

## Separate RadioTEDU migration and live-source verification

- The new program runs from the separate `RadioTEDU-OnAir-Radio` checkout on
  loopback port `18110`; the installed legacy OnAir process remains untouched.
- A SQLite online backup and DPAPI credential-store copy preserved 7 stations,
  37,462 tracks, 8 outputs, 4,705 queue rows, and existing media-path roots.
- Station 1 is running with a worker, FFmpeg program source, Icecast sink, and
  zero queued/dropped PCM chunks. The encoder begins writing `/radiotedu2`,
  but the remote host repeatedly terminates it with Windows socket error
  `-10054`; this is not an accepted stable source connection.
- Station 5 was also started and verified with a running worker and Icecast
  sink. A legacy Icecast `SOURCE` handshake was tested for stations 1 and 5,
  did not stop the remote resets, and was reverted to preserve the migrated
  configuration.
- The clean final backend from commit `22aa40e` was then started on port 18110.
  Stations 1, 2, and 5 were restored with process-isolated workers and active
  FFmpeg encoders. Secret-free one-shot commissioning samples passed for all
  three; station 2 initially showed zero encoder errors, while the station 1
  and 5 counters continued to expose remote reset recovery churn.
- The first API-only 60-second run (`7` samples, `0` HTTP failures) was found
  to be insufficient because it did not reject output recovery churn. The
  stricter replacement run recorded `7` samples, `6` failed stability gates,
  `9` recovery-attempt increases, and `54` new encoder errors. Evidence:
  `run/new-program/logs/soak-strict-source-60s.jsonl`.
- A final 60-second station-2 `/lofi` public-listener run kept local playout
  healthy but failed all `7` listener samples with connection resets. Encoder
  errors increased from `48` to `84` and recovery attempts from `16` to `28`.
  Evidence: `run/new-program/logs/commission-lofi-public-60s.jsonl`.
- A reversible 60-second SHOUTcast-source compatibility trial also failed all
  7 listener/runtime samples (`RemoteDisconnected`). The clone was restored to
  its migrated Icecast protocol and a subsequent local commissioning sample
  passed. Evidence: `run/new-program/logs/commission-lofi-shoutcast-60s.jsonl`.
- Managed-folder verification now scales to the real catalog. Station 1
  reconciled `5,822` files in `19.5` seconds with no metadata reprobes.
  Station 9 reconciled `10,861` playable files in `24.9` seconds while
  reporting and skipping three corrupt/zero-duration files. No media file or
  track row was deleted, and the pre-test online SQLite backup is retained at
  `run/new-program/backups/cleanroom-before-large-sync.db`.
- The remote listener endpoints currently reset or close HTTP audio probes, so
  public listener audio-byte verification remains an external commissioning
  blocker even though the source encoder is active.
- Packaged startup completed and the migrated SQLite clone reports
  `quick_check=ok`, WAL, FULL synchronous mode, and foreign keys enabled. The
  readiness endpoint remains fail-closed because 14.5 GB free is only about
  1.52% of the C: volume, below the 3% reserve threshold.

## Legacy Broadcast Wall isolation

- The three legacy Broadcast Wall scheduled tasks were disabled.
- Unattended legacy `/start`, `/tick`, `/supervise`, and `/loop/start` calls
  now return `409` unless explicit unattended-start authorization is enabled.
- A detached SYSTEM-owned legacy Python process could not be terminated from
  the current non-elevated session. It was functionally quarantined by the
  endpoint guards and cannot start a station. Because its scheduled tasks are
  disabled, it will not return after the next Windows restart.

## Legacy-data safety check

The installed legacy SQLite database was opened read-only and copied through a
consistent SQLite snapshot into temporary staging. The dry run succeeded
without replacing a target database:

- Stations: 7
- Tracks: 37,462
- System settings: 36
- Station settings: 317
- Station outputs: 8
- Ad break sets: 5
- Ad campaigns: 5
- Target replaced: no
- Source snapshot used: yes

The only warning was the absence of the optional legacy `schedule` table.

## Release artifact

- Installer: `release/setup/RadioTEDU-OnAir-Setup-1.0.2.exe`
- Size: 484,896,969 bytes
- SHA-256:
  `187757B5EA8E2981D9418EDD59E2BF087FE0E41016FF90524EBEDD7D4B2E64A5`
- Checksum sidecar:
  `release/setup/RadioTEDU-OnAir-Setup-1.0.2.sha256`
- Checksum comparison: matched
- Focused release validator: passed for 2,615 staged files
- Final packaged backend smoke: passed on isolated port 18113, covering login,
  UI assets, health, station, library, queue, scheduler, advertising, logs, and
  music-usage endpoints; readiness remains intentionally blocked by the 3%
  disk-reserve gate on this host
- Authenticode status: `NotSigned`

An organizational Authenticode certificate is still required to remove the
Windows unknown-publisher warning.

## Installer acceptance status

- The installer compiles successfully and its focused release validator covers
  2,614 staged RadioTEDU files. It is unsigned (`NotSigned`).
- The clean-install acceptance script correctly fails closed under the current
  non-administrator account. An elevated clean install, upgrade, rollback, and
  uninstall cycle remains required before replacing the legacy installation.

## Offline restore acceptance status

- A verified DPAPI recovery point can be staged through the admin recovery API
  without replacing the active database. Staging rechecks the stored SHA-256,
  SQLite integrity, and foreign keys before atomically publishing the bounded
  supervisor plan.
- The supervisor applies that plan before backend startup, retains the previous
  database under `Backups`, removes stale WAL/SHM files, records completion
  evidence, and restores the prior target on application failure.
- Disposable Python recovery/backup/ACL tests passed `19/19`; supervisor activation and
  rollback tests passed `3/3`. The production ProgramData drill remains part of
  the elevated cutover gate.

## Historical repository integration evidence

The following subsection is retained as historical evidence from the
2026-07-30 commissioning audit; it is not substituted for the current 1.0.2
installer/migration acceptance gates.

Verified on 2026-07-30 against the pinned RadioTEDU AI, Voting, and Juke
repositories:

- Current OnAir broad suite: 776 passed, 3 skipped, and 3 subtests passed in
  1,195.85 seconds.
- Current OnAir control-plane regression: 19 passed, including fixed service
  commands, signed Juke health, protected handoff provisioning, Ollama model
  validation, guarded fast-forward repository updates, database guards, and
  deterministic wall contracts.
- AI Radio: 422 backend tests passed, 1 skipped; 14 frontend tests passed; the
  production frontend build completed.
- Voting: 364 backend tests passed with 2 skipped; 74 local-agent tests passed;
  both managed packages compiled.
- Juke: 285 backend tests and 9 media-agent tests passed; the backend compiled
  after repairing its reproducible lockfile.
- Visible Edge mouse run: all 7 task menus activated with pointer coordinates;
  the TRT Radyo Haber preset populated the emergency source; emergency takeover
  armed without transmission; 7 service cards loaded; Check All completed; the
  database, repository, and Ollama-model two-click guards worked; settings
  saved; zero browser console errors; all autostart switches remained off.
- Exact-value scan: none of the private handoff's credential values occur in
  tracked source across OnAir, AI, Voting, or Juke.

The verification did not start any AI, Voting, or Juke service and did not
touch the live `/lofi` broadcast. AI speech remains intentionally blocked by
the upstream approval gate until commissioned RadioTEDU voice references are
supplied.

## Immediate-save and unattended-restart acceptance (2026-08-10)

- Focused high-assurance and station-output persistence tests: 27 passed.
- Browser-client JavaScript contract tests: 70 passed.
- Requested station-1 mount change: `/radiotedu2` to `/classic`.
- Draft validation and protected apply completed in 0.91 seconds; authoritative
  readback returned `/classic` and the protected password remained configured.
- The running worker reloaded the destination and reported metadata delivery
  for `/classic`. After the final restart, public FFprobe checks read AAC 48 kHz
  stereo from both `/classic` and `/lofi`; independent encoded samples received
  138,653 and 166,521 bytes. These were transient recovery samples: the next
  fail-closed commissioning attempts received connection resets and correctly
  failed. Station 5 remains uncommissioned because its `/radiotedu4` listener
  returns 404 and the source receives 401 Unauthorized.
- After a complete stop and launcher-only restart, stations 1, 2, and 5 all
  reached worker-running, program-running, active-media-input, and
  FFmpeg-output-running state without a station-start helper call.
- The staged packaged backend serving this behavior has SHA-256
  `A0ABE71FD8A13C932910DFE54D8F6190A08D3F1163DD20778CECBE9EED2AF952`
  and was built from clean source commit `ed80e925afd923f15448a5e9e88a844e551409da`.

## Release limitations

- The public installer is not Authenticode-signed.
- No claim is made that software testing can establish military or NATO
  certification. The evidence in this report is the reproducible reliability
  basis for this release.
