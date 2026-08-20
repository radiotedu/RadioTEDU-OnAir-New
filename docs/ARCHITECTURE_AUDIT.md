# RadioTEDU Broadcast Wall Architecture Audit

Audit date: 2026-07-29

This document records the read-only audit of the installed RadioTEDU Broadcast
Wall before replacement work began. Credential values were deliberately not
read into reports, copied into the project, or printed.

## Audited installation

- Installed location:
  `%LOCALAPPDATA%\Programs\RadioTEDU Broadcast Wall`
- Installed Git branch: `codex/picard-metadata-integration`
- Installed commit: `0ae6d962bbd2e42ce5305aaf5afb5f2be84b651b`
- Public clean upstream used to reconstruct the source/build tree:
  `radiotedu/rtai-broadcast-wall` at
  `a5fd90f164aab73a56fde0ca1f37b99e9c15a2f9`
- Replacement source tree:
  `<repository>\RadioTEDU-OnAir`

The installation itself was not modified or deleted. Full snapshots of the
installed application source and the clean upstream checkout were retained
outside the replacement repository while their differences are ported and
tested.

## Existing architecture

The existing product is a hybrid Windows desktop application:

1. A FastAPI backend owns authentication, station configuration, SQLite
   persistence, media operations, queues, scheduling, runtime state, Icecast
   output, local monitoring, WebSocket events, browser microphone ingestion,
   and optional AI services.
2. A static HTML/CSS/JavaScript operator dashboard is served by the backend.
3. A .NET 8 WPF/WebView2 desktop shell hosts that dashboard.
4. A .NET tray agent starts, monitors, restarts, and stops the packaged backend
   and desktop shell.
5. FFmpeg/ffprobe/ffplay and a custom Icecast source client implement encoding,
   probing, local playback, and source upload.
6. SQLite is the authoritative store for stations, outputs, settings, tracks,
   queues, playout state, schedules, users, roles, sessions, shows, studios,
   advertisements, soundboard items, and operation logs.

The installed database contained seven station profiles, eight station-output
rows, 15,106 tracks, 435,803 historical queue rows, eight persisted playout
states, and 18,793 operation-log rows at audit time.

## Existing streaming pipeline

The operational path is:

`queue/schedule -> station worker -> FFmpeg/GStreamer decoder -> PCM fan-out
and mixer -> Icecast encoder/source client + local monitor`

The runtime registry and station supervisor persist playout state, select
continuity fallbacks, push metadata, and attempt component recovery. Browser
microphone audio uses WebRTC when available and falls back to authenticated
WebSocket MediaRecorder chunks decoded by FFmpeg and mixed into the station
runtime.

## Existing storage

The installed program mixes immutable and mutable concerns:

- Executables, .NET runtime files, source code, tests, installer helpers, Git
  metadata, logs, downloaded media, generated screenshots, and ad-hoc scripts
  coexist in the installed directory under `AppData\Local\Programs`.
- The active database, database backups, runtime logs, generated audio, tool
  downloads, caches, and a JWT key are under
  `%LOCALAPPDATA%\RadioTEDU Broadcast Wall`.
- A second stale database plus an initial administrator password file and JWT
  key existed under the installed `_internal\data` directory.
- No RadioTEDU application data root existed under `%ProgramData%`.

The replacement layout is therefore:

- Immutable application and bundled tools:
  `%ProgramFiles%\RadioTEDU\OnAir`
- Shared station database, managed media, queues, schedules, logs, and crash
  state: `%ProgramData%\RadioTEDU\OnAir`
- Per-user UI settings and Windows-protected credential material:
  `%LOCALAPPDATA%\RadioTEDU\OnAir`
- Source code: a normal development folder outside every installed path

## Credential findings

The legacy `station_outputs` schema contains an `icecast_password` text column.
The replacement must migrate that value without exposing it, store new
credentials using Windows-protected per-user storage, and retain only a
credential reference in shared configuration. The legacy JWT key and initial
administrator password files are explicitly excluded from Git.

No database, credential file, private key, JWT, real stream password, log,
generated media, model, or packaged executable was copied into the new Git
repository.

## Observed failure modes

The existing logs contain repeated evidence of operational—not random—failure:

- 16,849 unsupported WebSocket upgrade attempts followed by `/ws` 404s.
- Thousands of remote Icecast source resets and failed source-kick requests.
- 574 failures where neither GStreamer nor FFmpeg could be resolved.
- 312 failures to start the silence continuity fallback.
- 270 PCM fan-out failures caused by reading a closed pipe.
- 119 PCM fan-out failures caused by an invalid/null memory view.
- Repeated immediate uploader exits, mount conflicts, rejected/absent mounts,
  and metadata-update failures.
- Optional Ollama/AI connection failures leaked into routine logs even though
  core broadcasting must remain independent of AI.
- The installed worktree contains many broad exception handlers, including
  handlers that discard exceptions; these are audit targets because they can
  produce silent or weakly diagnosed failures.

Long filenames and malformed media are not the sole root cause. The evidence
shows dependency discovery, source ownership, pipeline lifetime, network
recovery, mount validation, and test isolation are equally important.

## Immediate controls established

- The original installation remains untouched.
- The replacement has an independent Git history.
- The clean public upstream supplies reproducible build, desktop, installer,
  documentation, and test sources.
- Current installed source is retained separately for selective, test-backed
  porting rather than copied wholesale across incompatible schema revisions.
- Every test receives an isolated SQLite database so authentication, queues,
  and schema state cannot leak into another test or into operator data.
- Git exclusions cover databases, WAL/SHM files, media, logs, keys,
  certificates, bootstrap passwords, credential exports, models, build output,
  and packaged executables.

## Open audit gates

- Inspect and port the installed Icecast reconnect/source-client hardening.
- Replace plaintext output passwords with a credential-vault abstraction and
  a non-destructive migration.
- Move shared runtime state to ProgramData and immutable tools to Program
  Files.
- Add deterministic managed-folder ingestion and import status.
- Verify dashboard controls against authoritative read-back.
- Exercise real microphone devices and mouse/keyboard GUI flows.
- Test only against a non-production Icecast mount unless explicit permission
  is granted for live transmission.
