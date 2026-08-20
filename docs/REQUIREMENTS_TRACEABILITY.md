# Delivery Requirements and Evidence

This checklist is the completion contract for RadioTEDU-OnAir. An item is not
complete until the named evidence exists and has been inspected.

| Requirement | Required evidence | Status |
| --- | --- | --- |
| Original installation preserved | Read-only audit plus successful replacement acceptance report | **Verified locally.** Installed-tree Git status remained 7 tracked changes/17 untracked files before and after; no replacement, deletion, or write was performed. |
| Clean independent repository | Git root outside installed path; secret scan; reproducible build manifests | **Verified pre-publication.** Repository is under `Documents`, transient artifacts are ignored, source secret scan is clear, and the installer checksum is reproducible. |
| Program Files / ProgramData / user-profile separation | Runtime-path tests and installed-machine inspection | **Verified by source, tests, packaged smoke, and installer contract checks.** An elevated clean-host install/uninstall cycle remains pending because the current operator account is non-administrator. |
| Existing station/data migration | Backup, dry-run report, station counts, credential-vault migration test | **Verified on an isolated live clone.** Consistent read-only snapshot preserved 7 stations, 37,462 tracks, 8 outputs, 4,705 queue rows, and the protected credential vault; the installed target was never replaced. |
| Deterministic playout and explicit seeded shuffle | Unit/property tests and persisted transition log | **Verified.** Deterministic queue, generation, shuffle, event, and transition tests passed. |
| No silent failures | Structured error contract, log assertions, fault-injection report | **Verified.** Structured API/runtime errors, bounded recovery, and fault-path tests passed. |
| Media validation and managed folders | Songs/jingles/IDs/ads/shows folder tests and dashboard screenshots | **Verified.** Media validation/import summaries, managed-folder APIs, and dashboard controls passed automated and rendered checks. |
| Automatic ingestion | File-watcher integration tests including changes, duplicates, long names, and malformed files | **Verified.** Stable-file, duplicate, recursive, malformed, and rescan watcher tests passed. |
| Playlist and jingle automation | Rule-engine tests for every-N-songs, time, priority, cooldown, and conflicts | **Verified.** Playlist, queue, jingle, advertisement, schedule, priority, and conflict tests passed. |
| Live microphone | Device selection, permission, meter, gain, PTT/live, ducking, and disconnect tests | **Automated verification complete; live transmission permission-gated.** Python/JavaScript mic and disconnect tests passed; no production microphone stream was opened. |
| Multi-station onboarding | RadioTEDU safe connection test plus independent generic station test | **Local multi-station playout verified; public source acceptance blocked externally.** The final backend ran isolated station workers and real media for stations 1, 2, and 5. Their FFmpeg source encoders were started, but the remote server reset the connections. |
| Complete dashboard control | Real mouse/keyboard end-to-end control inventory | **Verified for the operator inventory.** Visible mouse/keyboard tests covered station, output, queue, library, AI, emergency, live-input, service, diagnostics, settings, and jingle workflows; destructive production replacement remains gated. |
| Voting | Compatible API adapter, operator controls, validation, failure/degraded-state tests | **Verified.** Adapter, dashboard, validation, and degraded-state tests passed. |
| Study/mobile integration | Documented adapter or intentionally scoped link with degraded-state tests | **Verified.** Optional adapter is documented and fails closed without affecting playout. |
| Optional AI | Local provider controls, no-core-dependency proof, outage tests | **Verified.** AI controls, cache/readiness, disabled mode, timeout, and outage behavior passed; core playout remains independent. |
| Recovery | Network loss, encoder exit, restart, wrong credentials, crash state, offline restore, and rollback tests | **Component chain verified; installed drill pending.** Runtime fault tests passed. A protected recovery point can now be reverified and atomically staged without touching the live DB; the supervisor verifies its hash, activates it before backend start, retains the prior DB, and rolls back on failure. Python recovery tests passed 18/18 and supervisor apply/rollback tests passed 3/3. An elevated installed-machine drill remains required. |
| Installer/uninstaller | Built installer, clean install, upgrade, uninstall, and data-retention report | **Package and contract verified; elevated clean install/upgrade/uninstall remains pending.** The installer is unsigned and must not replace the legacy product until an elevated migration/rollback drill succeeds. |
| Operator documentation | Operator guide, troubleshooting guide, configuration reference | **Verified.** Dedicated operator, troubleshooting, configuration, release, and test-report documents are linked from the README. |
| Branded delivery | RadioTEDU assets with provenance and rendered README/app screenshots | **Verified.** RadioTEDU branding is rendered in the app and `docs/assets/onair-dashboard.png` is embedded in the README. |
| GitHub publication | Visibility decision, license approval, clean secret scan, successful push | **Permission-gated.** Secret scan passed; visibility choice, initial commit, repository creation, and push remain. |

## Required final acceptance audit

| Objective-file gate | Current authoritative evidence | Result |
| --- | --- | --- |
| Clean installation on Windows | Installer and clean-install verifier built; current account is non-administrator and verifier fails closed before mutation. | **Pending elevated run.** |
| Exactly one canonical installation | Separate 1.0.2 clone runs on 18110 while the preserved legacy installation remains on 8100 by explicit safety requirement. | **Pending stability acceptance and cutover.** |
| Exactly one supervisor/backend instance | Single-instance controls are tested, but legacy and clone backends intentionally coexist until cutover. | **Pending cutover.** |
| Independent operation of every configured station | Seven configs migrated; real isolated workers verified for stations 1, 2, and 5. | **Partial; stations 4, 7, 8, and 9 still require commissioned output checks.** |
| Real source connection confirmed | Real FFmpeg source processes start and emit encoded PCM, but `stream.radiotedu.com:11154` closes source/listener sessions with `-10054`. | **Failed external gate.** |
| Current track confirmed through API | Strict commissioning evidence reports `track_present=true` with real catalog paths and active worker heartbeats. | **Verified.** |
| 24-hour soak facility and initial monitored test | Fail-closed soak runner exists; multiple 60-second monitored runs were performed. The final `/lofi` run failed all 7 public-listener samples and recorded 36 new encoder errors. | **Facility verified; stability acceptance failed.** |
| Zero unexplained source reconnects | Recovery and encoder counters rose during every sustained public-source observation. | **Not achieved.** |
| No secrets committed to Git | Tracked-tree secret scans and release-scope validator are clear; reports and diagnostics redact credentials. | **Verified.** |
| All tests passing | Current full Python/integration run: 1,026 passed, 3 expected Playwright-plugin skips, and 3 subtests passed; JS: 70/70; .NET: 54/54; installer handoff static scripts passed; final packaged backend smoke and 2,614-file release validation passed. | **Current source tree and package verified within the non-elevated gate.** |
| Rollback and recovery procedure tested | Atomic staging, supervisor activation, pre-restore backup, failure rollback, and origin evidence are covered by Python/.NET tests. | **Component drill verified; elevated installed-machine drill pending.** |

## Remaining external decisions

1. Provide administrator elevation for the clean install, migration, service,
   cutover, rollback, and uninstall acceptance cycle.
2. Correct or authorize access to the remote Icecast/relay source on port
   11154, then repeat public listener verification and the 24-hour soak.
3. Free enough C: capacity to satisfy the configured 3% production reserve.
4. Provide an organizational Authenticode certificate before describing the
   installer as production-signed or publishing it as a signed binary.
