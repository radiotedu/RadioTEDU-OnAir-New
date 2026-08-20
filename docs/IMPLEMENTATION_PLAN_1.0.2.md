# RadioTEDU OnAir 1.0.2 implementation plan

Status: approved implementation sequence pending execution
Prepared: 2026-08-09

This plan implements the architecture and closes the risks in
`ARCHITECTURE_1.0.2.md` and `THREAT_FAILURE_ANALYSIS_1.0.2.md`. Phases are
gated. A later phase cannot reinterpret an earlier failed gate as success.

## Global rules

- Preserve the current broadcast, database, media, credentials, legacy
  programs, and rollback material.
- Do not print or copy secret values into source, tests, commands, logs,
  screenshots, or reports.
- Do not delete the untracked archive, legacy applications, scheduled tasks,
  or recovery snapshots without explicit approval and the final retirement
  gate.
- Use non-production protocol fixtures for destructive and failure-injection
  tests. A production mount is used only for read-only listener verification or
  an explicitly authorized source test.
- Before every data/ACL/service mutation, create and verify a recoverable
  snapshot appropriate to that mutation.
- A passing compile is evidence for only the compile gate.

## Phase 0 ? preservation and reproducible baseline

Deliverables:

1. Record Git commit, dirty paths, installed version, process/service/task
   counts, database schema/integrity, ACL summaries, disk headroom, station
   state, and public listener evidence without secret values.
2. Create a verified online database backup and a configuration/ACL/service
   manifest under the protected recovery root.
3. Hash active binaries, dependency lock files, installer inputs, and the
   protected recovery manifest.
4. Run the current unit/integration/JavaScript/.NET/installer-static suites and
   record failures as baseline defects.
5. Add a build preflight that refuses migrations, installer builds, or restores
   below configurable free-space thresholds.

Acceptance:

- the live station remains live throughout read-only baseline collection;
- database `quick_check` is `ok` and all existing foreign-key violations are
  recorded before repair;
- every backup copy passes `quick_check` and `foreign_key_check` with the
  expected pre-repair result;
- no secret canary or known credential pattern appears in the manifest; and
- rollback paths are readable by the recovery identity.

## Phase 1 ? close immediate data and security blockers

Deliverables:

1. Add a transactional schema migration that repairs or quarantines the nine
   orphaned role assignments deterministically and adds a regression invariant.
2. Harden ACLs for JWT, bootstrap credentials, the DPAPI vault, backups,
   service definitions, logs, crash data, and supervisor control state.
3. Rotate JWT material if the pre-hardening exposure assessment cannot prove
   confidentiality; invalidate existing refresh sessions through an audited,
   operator-visible migration.
4. Enforce bounded session retention and store only non-replayable refresh-token
   verifiers.
5. Export and disable the two conflicting legacy Classical boot starts while
   preserving their definitions and files for rollback.
6. Correct optional-service config parsing and invalid-MP3 warning storms; add
   log signature rate limiting.

Acceptance:

- both SQLite integrity and foreign-key checks return clean;
- untrusted local Users cannot modify or read protected secret material;
- the service identity can decrypt all five source credentials;
- a reboot produces one canonical backend and no legacy source process;
- optional services can fail without changing station output health; and
- seeded secrets are absent from logs and diagnostic output.

## Phase 2 ? canonical supervisor and process-isolated station workers

Deliverables:

1. Extend the .NET service host into the single RadioTEDU machine supervisor or
   add a dedicated supervisor executable using the same tested primitives.
2. Move backend ownership from the interactive agent to the Windows service.
3. Add a station-worker CLI/process entry point with a narrow configuration
   contract and no secret command-line arguments.
4. Add named mutexes, Windows job objects, process generations, station leases,
   fencing tokens, heartbeats, graceful deadlines, restart budgets, backoff,
   jitter, and circuit breakers.
5. Move station audio/runtime ownership out of backend threads. Keep the
   backend as control plane and event aggregator.
6. Add durable command-outbox acknowledgements and idempotent desired-state
   reconciliation.

Acceptance:

- exactly one supervisor and backend exist after launch, crash, repair, and
  reboot;
- every running station has exactly one worker process and fenced lease;
- killing one worker does not interrupt another station;
- killing the backend does not stop ongoing program/source byte flow;
- stale workers cannot reconnect or control a station after replacement;
- operator Stop survives all watchdog and reboot paths; and
- queue/track/offset recovery occurs without destructive queue clearing.

## Phase 3 ? output protocol and audio-path completion

Deliverables:

1. Retain and refactor the hardened Icecast adapter behind a common output
   interface.
2. Add a Shoutcast-compatible legacy source adapter for configured MP3/AAC
   DNAS source ports, with bounded handshake/read/write timeouts and explicit
   capability reporting.
3. Add Icecast and Shoutcast protocol simulators for accept, reject, wrong
   password, duplicate source, half-open, slow-read, reset, empty-payload, and
   recovery contracts.
4. Separate decode heartbeat, PCM queue health, encoder byte progress, source
   handshake, and independent listener verification.
5. Complete measurable silence, clipping, invalid-file, loudness, cue,
   intro/outro, crossfade, and fallback behavior.
6. Confirm that live takeover and safe return to automation are durable state
   transitions, not UI-only state.

Acceptance:

- real or authorized fixture outputs pass the full adapter contract;
- no PID-only or socket-only condition reports `live`;
- half-open and zero-payload sources become degraded and recover within policy;
- a blocked output cannot block decoding or another output;
- wrong credentials open a bounded circuit without exposing the value;
- emergency/fallback audio is locally available with network and library
  unavailable; and
- return from live input resumes the preserved automation checkpoint safely.

## Phase 4 ? programming, media, and schedule completion

Deliverables:

1. Verify or complete media import, hashing/duplicate policy, metadata editing,
   indexing/search, long-path handling, stable-file ingestion, quarantine, and
   invalid-file reporting.
2. Verify or complete playlists, queues, rotations, clocks, categories,
   weighted selection, repeat protection, and deterministic seeded shuffle.
3. Verify seven-day schedules and editable station-specific dayparts with IANA
   timezone data and explicit DST ambiguity policy.
4. Verify station-specific BPM ranges and measurable loudness targets.
5. Verify sweepers, IDs, jingles, ads, shows, scheduled events, cue points,
   intro/outro markers, and priority/conflict rules.
6. Keep AI, voting, and request modules independently disabled and isolated.

Acceptance:

- every objective capability maps to source, a state/API contract, an
  automated test, and operator documentation;
- station selection never leaks tracks or queue items across stations;
- schedule boundary and both DST transitions are deterministic;
- corrupt/missing/locked/slow media cannot stop fallback playout; and
- optional-provider outage has no synchronous dependency in core playout.

## Phase 5 ? backup, crash evidence, recovery, and diagnostics

Deliverables:

1. Add scheduled online SQLite backups, integrity/foreign-key verification,
   bounded retention, free-space preflight, and last-verified status.
2. Add staged restore, atomic activation, rollback, export, and disaster-
   recovery commands.
3. Configure bounded per-executable WER LocalDumps in a protected directory.
4. Add a redacted diagnostic-bundle generator with an allowlist schema and
   optional separately consented crash-dump attachment.
5. Add restore drills and diagnostic self-tests.

Acceptance:

- live backup succeeds under concurrent read/write load and verifies clean;
- corruption, lock, missing path, full disk, and interrupted restore leave the
  prior active database recoverable;
- backup restoration reproduces station/queue/schedule counts and protected
  credential references;
- no secret canary appears in a standard diagnostic bundle; and
- crash retention is bounded and protected by effective ACL tests.

## Phase 6 ? coherent UI and truthful operations

Deliverables:

1. Provide the RadioTEDU sections: On Air, Stations, Media, Queue, Scheduler,
   Dayparting, Automation, Emergency, Services, Diagnostics, Settings, and
   Backup and Recovery.
2. Use one state vocabulary: Playing, Connected, Live, Degraded, Recovering,
   Failed, and Stopped, each derived from backend evidence.
3. Add station-specific diagnostics, event history, recovery budget, output
   evidence, last backup, storage risk, and secret-safe export controls.
4. Keep WebView2 standard-user, origin-restricted, and minimal at its native
   bridge.
5. Make destructive actions require read-back, confirmation, idempotency keys,
   and audit events.

Acceptance:

- all required sections open and work by real mouse/keyboard;
- every mutation is permission-tested and confirmed through authoritative
  read-back;
- closing the shell does not change source media flow;
- a scheduler/encoder PID alone can never render Live; and
- browser console, native shell, and backend contain no unexplained errors.

## Phase 7 ? installer, repair, update, and rollback

Deliverables:

1. Establish one version source that emits 1.0.2 into API metadata, .NET
   assemblies, installer, registry, provenance, release notes, and filenames.
2. Install immutable binaries to Program Files, shared state to ProgramData,
   and UI data to the user profile with exact ACLs.
3. Install one supervisor service, remove conflicting canonical-start paths,
   and preserve legacy definitions in the rollback manifest.
4. Implement repair verification, signed-update-ready payload verification,
   upgrade backup, rollback, and clean uninstall with retained data by default.
5. Produce reproducible source/dependency/tool hashes and SHA-256 assets.

Acceptance:

- clean install, launch, service start/stop, repair, upgrade, forced-failure
  rollback, and uninstall pass on a clean Windows environment;
- install and repair result in exactly one canonical application and supervisor;
- data and credential vaults survive upgrade and default uninstall;
- binaries, registry, API, provenance, installer, and release notes agree on
  1.0.2;
- unsigned artifacts are labeled unsigned until an organizational
  Authenticode certificate is used; and
- the installer is built from the exact tested commit with no later source
  changes.

## Phase 8 ? full verification and controlled commissioning

Automated suites:

- Python unit, integration, contract, migration, recovery, property, and
  failure-injection tests;
- JavaScript UI/API-contract tests;
- .NET supervisor/shell/service tests;
- installer static and real lifecycle tests;
- secret/history/dependency vulnerability scans; and
- build, packaging, provenance, and rollback tests.

Installed-machine tests:

- startup/shutdown, backend crash, worker crash, encoder crash, network loss,
  half-open connection, rejection, wrong password, slow/unavailable storage,
  corrupt/missing media, database lock/corruption, concurrent commands,
  schedule/DST, optional-provider outage, reboot, upgrade, rollback, and backup
  restore.

Commissioning evidence:

1. exactly one installed application, supervisor, and backend;
2. one isolated worker for every enabled station;
3. source connection confirmed at the server/adapter;
4. non-empty listener media bytes received repeatedly;
5. current track confirmed through the API and consistent with worker state;
6. a configurable 24-hour soak facility that records periodic API, listener,
   process-generation, byte-progress, reconnect, disk, and backup evidence;
7. an initial monitored run completed and reviewed; and
8. zero unexplained source reconnects in the claimed observation interval.

## Phase 9 ? stability and legacy retirement gate

1. Mark 1.0.2 stable only after all prior gates pass and the release evidence is
   reviewed.
2. Let the canonical 1.0.2 installation operate through the agreed stability
   window with monitored broadcasting and a tested rollback path.
3. Reconcile every station, media root, schedule, optional-service config,
   account/role, output credential reference, and startup definition against
   the preserved legacy manifest.
4. Present a precise retirement list and recovery location to the operator.
5. Delete previous programs only after explicit approval. Preserve user data
   and the final verified recovery package according to the agreed retention
   policy.

The phrase "stable" in this plan means all measurable gates above passed. It
does not mean zero defects or unlimited availability.
