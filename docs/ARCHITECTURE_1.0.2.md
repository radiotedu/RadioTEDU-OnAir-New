# RadioTEDU OnAir 1.0.2 architecture

Status: implementation gate
Prepared: 2026-08-09
Scope: the canonical Windows installation, supervisor, API, station workers,
audio/output processes, operator UI, data, recovery, and optional modules.

This document defines the architecture that must be true before RadioTEDU
OnAir 1.0.2 can be called stable. It does not claim zero defects, formal safety
integrity certification, NATO certification, or a measured availability level
that has not been demonstrated.

## Safety invariants

The following are release-blocking invariants:

1. Closing the UI cannot stop playout or source output.
2. There is exactly one machine supervisor and one backend/API instance.
3. Each station has exactly one station-worker process and one station lease.
4. A failed station worker cannot terminate or corrupt another station.
5. A station may have multiple configured outputs, but at most one active
   source connection for each destination identity.
6. Operator Stop is authoritative. Recovery never restarts an intentionally
   stopped station.
7. Recovery preserves the queue, current item, and durable playback position.
8. `live` means the worker is decoding program audio, the required encoder is
   feeding, the source is accepted, and an independent listener probe has
   received non-empty `audio/*` bytes. A PID or TCP connection is not enough.
9. Core playout continues when the UI, AI, voting, request services, metadata
   service, library watcher, or network API is unavailable.
10. Secrets never appear in Git, configuration values, process arguments,
    logs, screenshots, diagnostic bundles, or reports.
11. Every destructive migration has a verified backup and a tested rollback.
12. Legacy programs are preserved and disabled from conflicting starts until
    1.0.2 passes the stability gate. They are not deleted automatically.

## Current-state evidence

The repository already contains a substantial working product: a FastAPI
backend, SQLite store, static operator application, .NET 8 WebView2 shell and
agent, service-host executable, FFmpeg-based audio paths, Icecast source
output, station scheduling, RBAC, migration tools, an Inno Setup installer, and
more than 200 Python/JavaScript/.NET test files.

The read-only audit on 2026-08-09 found:

- one installed `RadioTEDU OnAir` application registered as 1.0.1;
- one running backend bound to loopback and one desktop agent;
- seven configured stations, with the Lo-Fi station broadcasting and the
  remaining stations stopped;
- a real listener response of HTTP 200, `audio/aac`, and 69,616 non-empty
  bytes, plus current-track metadata returned through the public API;
- five output rows containing `credential://user/...` references and a
  machine-scoped DPAPI vault containing five decryptable credentials;
- SQLite WAL mode, `synchronous=FULL`, schema version 16, and a successful
  `quick_check`;
- nine orphaned `user_role_assignments` foreign keys;
- only 2.31 percent free space on the system volume;
- a JWT key and bootstrap-password file inheriting Modify access for the local
  Users group;
- two enabled legacy Classical boot tasks that can start different historical
  playout commands after reboot;
- station workers implemented as backend threads, not independent processes;
- no Shoutcast output implementation or tests in the current source tree;
- no implemented application crash-dump and redacted diagnostic-bundle
  facility;
- repeated invalid-MP3 warnings from the optional voting agent and smaller
  configuration/WebSocket faults in optional services;
- a 714 MB untracked archive-like file in the repository root, which is
  preserved as user-owned data and excluded from all automated cleanup.

These findings make 1.0.2 a hardening and process-isolation release, not a
version-number-only release.

## Target process topology

```text
Windows Service Control Manager
  -> RadioTEDU OnAir Supervisor (single machine instance)
       -> Backend/API process (single instance, loopback by default)
       -> Scheduler coordinator (one active leader)
       -> Station worker process: station 1
            -> decoder/mixer process(es)
            -> encoder/source process per enabled output
       -> Station worker process: station 2
            -> decoder/mixer process(es)
            -> encoder/source process per enabled output
       -> ... one isolated worker per configured station
       -> Optional module supervisor
            -> AI service
            -> voting adapter
            -> request/media adapters

Operator shell (standard user, WebView2)
  -> authenticated loopback API

External streaming server
  <- Icecast or Shoutcast-compatible source connection
  -> independent listener probe
```

The supervisor, backend, and station workers use explicit command-line
contracts that contain only identifiers and protected-file references. Source
passwords are resolved inside the worker after startup and are never passed on
the command line.

## Component responsibilities

### Machine supervisor

The .NET Windows service is the only component allowed to create the backend
and station-worker processes. It owns:

- a named machine mutex and service identity;
- child-process job objects so descendants are contained and shut down
  deterministically;
- heartbeat deadlines, graceful-stop deadlines, restart budgets, exponential
  backoff with jitter, and circuit breakers;
- generation IDs so a stale child cannot rejoin after replacement;
- bounded, redacted, rolling stdout/stderr capture;
- readiness orchestration and reboot reconciliation;
- per-process Windows Error Reporting configuration; and
- a machine-readable supervisor snapshot consumed by diagnostics.

The supervisor does not own scheduling policy, queues, media selection, audio
mixing, or stream credentials.

### Backend/API

The backend is the control plane and authoritative configuration API. It owns:

- users, roles, permissions, sessions, CSRF/origin checks, and audit events;
- station/output configuration and protected credential references;
- playlists, rotations, clocks, schedules, dayparts, ads, programs, emergency
  policy, media metadata, and validation results;
- transactional migrations, backups, restore orchestration, and exports;
- desired station state and commands written to a durable command outbox; and
- aggregation of supervisor, worker, encoder, and listener evidence.

The backend does not decode or mix program audio. Its failure must not stop an
already-running station worker.

### Scheduler coordinator

The scheduler computes deterministic future commands from the seven-day
schedule, timezone database, dayparts, rotations, ad rules, and emergency
priority. It writes idempotent commands to the durable outbox. A lease and
fencing token ensure that only one scheduler generation commits commands.

### Station worker process

Each worker is independently restartable and receives only its station ID,
database path, supervisor endpoint, and generation token. It owns:

- the station lifecycle state machine;
- queue consumption and durable playback checkpoints;
- deterministic selection, BPM/daypart limits, cue points, crossfades,
  sweepers, IDs, ads, programs, emergency fallback, and live takeover;
- decoder/mixer/encoder child processes and bounded PCM queues;
- a stable, bounded memory-mapped PCM bridge for microphone, guest, program-minus,
  and soundboard control across backend replacement; stale bridge state becomes
  silence and triggers a checkpoint-preserving return to program audio;
- source ownership and output health; and
- structured station events and heartbeats.

The worker acquires a transactional station lease before touching audio or an
output. A fenced generation that loses its lease stops its encoders and exits.

### Encoder/output adapter

Outputs use a common contract:

```text
configure -> connect -> authenticate -> feed -> verify -> drain -> stop
```

1.0.2 supports two explicit source modes:

- Icecast HTTP source output, including TLS, mount-specific credentials,
  metadata updates, bounded write timeouts, and listener verification.
- Shoutcast-compatible legacy source output for MP3/AAC to a configured DNAS
  source port, with a protocol-specific handshake and an independent
  compatibility simulator/contract test. Shoutcast v2 Ultravox mode is not
  silently implied; it is reported as unsupported unless implemented and
  tested against an authorized DNAS fixture.

No adapter may infer success from an encoder PID. Required evidence is the
adapter handshake, forward byte movement, source-side health, and listener
media bytes.

### Optional modules

AI, voting, local requests, guest sessions, and media adapters run outside the
core backend and every station worker. Their service contracts use bounded
timeouts, bounded queues, explicit degraded states, and no synchronous calls
from the real-time audio path.

## Lifecycle state machines

### Station desired state

`stopped -> starting -> playing -> live/degraded -> recovering -> failed`

- `playing`: the program decoder is advancing.
- `connected`: the source handshake succeeded and bytes are moving.
- `live`: playing + connected + independent listener media verification.
- `degraded`: program continues but a noncritical branch or verification is
  unhealthy.
- `recovering`: a bounded recovery action is in progress.
- `failed`: the restart budget is exhausted or an operator-resolution circuit
  is open.
- `stopped`: an operator or durable policy requested stop.

Transitions are append-only audit events with station ID, generation, cause,
previous state, new state, retry budget, and redacted evidence.

### Recovery order

Recovery always uses the smallest safe action:

1. repeat a failed probe after hysteresis;
2. reconnect only the failed output;
3. recreate the failed encoder while keeping the decoder/checkpoint;
4. recreate the station runtime from the checkpoint;
5. restart the station-worker process;
6. open the circuit and use local emergency/fallback audio;
7. require explicit operator acknowledgement when the restart budget is
   exhausted.

Queue clearing is never a recovery action.

## Storage and transactions

```text
%ProgramFiles%\RadioTEDU\OnAir
  immutable signed-ready application and bundled tools

%ProgramData%\RadioTEDU\OnAir
  database, managed media, logs, checkpoints, backups, crash data,
  machine-scoped DPAPI vault, supervisor state

%LOCALAPPDATA%\RadioTEDU\OnAir
  WebView2 user-data folder and per-user UI preferences
```

SQLite remains the single-machine authority in 1.0.2. Every connection enables
foreign keys, WAL, a bounded busy timeout, and FULL synchronous commits.
Migrations run under an exclusive migration lease and a single transaction;
before migration, the SQLite online-backup API creates a verified backup.
Startup fails readiness if `quick_check`, `foreign_key_check`, schema version,
or required backup validation fails.

Configuration and state ledgers use create-new temporary files, file flush,
directory-safe atomic replace, a last-known-good generation, and schema/version
validation on read.

## Credentials and service identity

Source credentials are stored only in the ACL-restricted machine vault and
protected with Windows DPAPI machine scope. The database stores opaque
credential references. Machine scope is required for unattended service
recovery, but it is paired with strict ACLs because machine-scoped data can be
unprotected by other trusted accounts on the computer.

The UI runs as a standard user. The supervisor uses the least-privileged
service identity that can access required audio, storage, and network
resources; LocalSystem is not the default design target. Privileged installer
and repair operations are isolated from WebView2. The WebView2 host checks
navigation origin and exposes a minimal, validated native bridge.

JWT signing material and bootstrap credentials receive protected ACLs. The
bootstrap-password file is one-time material: after the first successful
administrator password change, it is securely retired through a recoverable
operator-confirmed workflow.

## Health and observability

- Liveness proves only that a process/loop is responsive.
- Readiness proves database integrity, migrations, protected credential
  access, required storage headroom, supervisor ownership, and configuration.
- Station health combines worker heartbeat, decode progress, playback clock,
  encoder byte progress, source handshake, and listener media probe.
- Logs are structured, redacted at creation, size/age bounded, and correlated
  with request, station, worker-generation, and output IDs.
- Diagnostic bundles contain inventories, state snapshots, hashes, bounded
  logs, database integrity output, and crash metadata. They exclude database
  contents, media, secrets, tokens, environment values, and raw configuration.
- WER dump retention is bounded per RadioTEDU executable and stored under a
  protected crash directory. Dumps are never added to normal support bundles
  without explicit operator consent because memory dumps can contain secrets.

## Backup, restore, upgrade, and rollback

- Automatic backups use SQLite's online backup API while the application is
  live, followed by integrity and foreign-key verification of the copy.
- Retention is bounded by count, age, and minimum free-space policy.
- Restore is staged to a new file, verified, and atomically activated only
  while workers are stopped at durable checkpoints.
- The installer never overwrites the only verified backup.
- Upgrade records the previous application manifest and keeps the prior
  binaries until post-upgrade acceptance passes.
- Rollback restores binaries and, only when required by schema compatibility,
  the matching verified pre-upgrade database backup.
- Uninstall removes binaries and services but preserves ProgramData by default;
  data deletion requires a separate explicit confirmation.

## Authoritative technology constraints

- FastAPI lifespan setup/cleanup follows the official
  [lifespan guidance](https://fastapi.tiangolo.com/advanced/events/).
- Uvicorn resource limits, graceful shutdown, and worker semantics follow the
  official [Uvicorn settings](https://www.uvicorn.org/settings/). RadioTEDU
  still uses one API process because in-memory runtime ownership is forbidden
  and station workers are separate processes.
- SQLite durability and checkpoints follow the official
  [WAL documentation](https://sqlite.org/wal.html),
  [PRAGMA integrity checks](https://sqlite.org/pragma.html#pragma_integrity_check),
  and Python's [online backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup).
- Icecast mounts, source authentication, and source-client separation follow
  the official [basic setup](https://icecast.org/docs/icecast-trunk/basic_setup/),
  [authentication](https://icecast.org/docs/icecast-latest/auth/), and
  [configuration](https://icecast.org/docs/icecast-trunk/config_file/) docs.
- Audio filters and network timeouts follow the official
  [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html) and
  [protocols](https://ffmpeg.org/ffmpeg-protocols.html) documentation. FFmpeg's
  `legacy_icecast` option is treated only as the documented pre-2.4 Icecast
  `SOURCE` method and is not misrepresented as a Shoutcast v1 implementation.
- The legacy DNAS adapter follows the documented password-first source
  handshake, optional `:#stream-id` password suffix, `OK2` acceptance, and ICY
  headers described by the
  [SHOUTcast DNAS Source Support reference](https://sc-mirror.shoutca.st/docs/DNAS_Server_Source_Support.html).
- The supervisor follows Microsoft's [.NET Windows Service guidance](https://learn.microsoft.com/en-us/dotnet/core/extensions/windows-service).
- DPAPI scope follows Microsoft's
  [`DataProtectionScope` guidance](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.dataprotectionscope).
- The shell follows Microsoft's
  [secure WebView2 guidance](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security).
- Crash capture follows Microsoft's
  [WER LocalDumps settings](https://learn.microsoft.com/en-us/windows/win32/wer/wer-settings).
- Installer privilege, signing, and uninstall behavior follow the official
  [Inno Setup help](https://jrsoftware.org/ishelp/contents.htm).

## Architecture acceptance gate

Architecture is implemented only when automated and installed-machine evidence
proves every safety invariant, process count, lease/fencing behavior, recovery
order, secret boundary, backup/restore path, protocol adapter, and UI state
definition. Compilation and mocked unit tests alone cannot close this gate.
