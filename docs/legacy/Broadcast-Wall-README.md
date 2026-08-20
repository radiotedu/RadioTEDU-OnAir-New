<p align="center">
  <a href="https://radiotedu.com">
    <img src="https://radiotedu.com/wp-content/uploads/2025/08/logo-03-scaled.png" alt="RadioTEDU" width="220">
  </a>
</p>

<h1 align="center">RadioTEDU Broadcast Wall</h1>

<p align="center">
  A Windows broadcast automation, playout, metadata, and operations console for RadioTEDU stations.
</p>

<p align="center">
  <strong>Multi-station playout</strong> | <strong>Icecast streaming</strong> | <strong>Queue automation</strong> | <strong>Metadata enrichment</strong> | <strong>Operator control</strong>
</p>

---

## Overview

RadioTEDU Broadcast Wall is the production control surface and backend runtime used to operate multiple RadioTEDU channels from a single Windows machine. It combines a desktop shell, a FastAPI backend, station workers, FFmpeg-based playout, Icecast output management, public now-playing endpoints, and library tooling for maintaining a clean broadcast catalog.

The repository is a full install snapshot. It includes the packaged desktop and backend executables, Python application modules, static frontend assets, runtime helpers, startup scripts, and regression tests. Local databases, credentials, generated logs, caches, and machine-specific secrets are intentionally excluded.

## Core Capabilities

- Multi-station broadcast automation with independent queues, station settings, and output configuration.
- FFmpeg-backed playout with Icecast streaming, local monitoring support, crossfade-aware runtime management, and continuity fallback.
- Operator console for library management, queue control, schedules, shows, ads, soundboard, studios, and runtime health.
- Public station lobby and now-playing API with title, artist, album, duration, cover art, stream URLs, and channel status.
- Cover-art discovery through embedded media artwork, sibling artwork files, and fallback branding.
- Startup and autostart helpers for unattended backend and playout guard operation on Windows.
- Regression tests for queue behavior, runtime recovery, metadata cleanup, and broadcast safety paths.

## Architecture

RadioTEDU Broadcast Wall is organized as a local desktop application with a backend service and a static web UI.

| Layer | Responsibility |
| --- | --- |
| Desktop shell | Packaged Windows shell/WebView host for the operator interface. |
| FastAPI backend | API routing, auth, station state, setup, queue/library actions, runtime control, and public endpoints. |
| Station workers | Per-station automation loops that maintain queues, advance tracks, recover runtimes, and keep fallback audio available. |
| Audio runtime | FFmpeg pipelines for local and Icecast output, stream metadata updates, transitions, and branch health reporting. |
| Metadata services | Tag reading, cover art, local cleanup tools, and database reconciliation. |
| Storage | SQLite runtime databases in local app data; generated state is intentionally outside version control. |

## Repository Layout

```text
.
|-- README.md
|-- radiotedu-broadcast-room-backend.exe
|-- radiotedu-broadcast-room-agent.exe
|-- shell/
|   `-- radiotedu-broadcast-room-shell.exe
|-- installer/
|   `-- prerequisite installers and setup helpers
|-- _internal/
|   |-- app/
|   |   |-- api/            # FastAPI route modules
|   |   |-- audio/          # FFmpeg/Icecast/local audio runtime
|   |   |-- engine/         # station workers, runtime registry, playout state
|   |   |-- metadata/       # local metadata helpers
|   |   |-- repositories/   # SQLite data-access layer
|   |   |-- services/       # background services and AI/prefetch helpers
|   |   |-- static/         # operator UI, lobby, icons, CSS, JS
|   |   `-- main.py         # FastAPI application entrypoint
|   `-- data/               # generated runtime data; excluded where sensitive
|-- tests/
|   `-- regression tests
`-- StartRadioTEDU*.ps1     # backend and playout-guard startup helpers
```

## Runtime Data and Security

The application stores live runtime state under the local app data directory, typically:

```text
C:\Users\<user>\AppData\Local\RadioTEDU Broadcast Wall\
```

The following are deliberately not tracked in Git:

- Live SQLite databases such as `cleanroom.db`, `music_history.db`, and `radio.db`.
- JWT secret keys and generated machine credentials.
- Icecast passwords, user sessions, station history, logs, caches, and hotfix backups.
- Temporary metadata scans, generated cover cache, and local tool state.

This keeps the repository safe to publish while preserving the ability for the application to recreate local runtime state through setup, migrations, and normal operation.

## Getting Started

### Prerequisites

RadioTEDU Broadcast Wall is packaged for Windows. The install snapshot includes or bootstraps the key runtime components used by the app:

- RadioTEDU backend and desktop shell executables.
- Python application modules under `_internal/app`.
- FFmpeg, FFplay, and FFprobe runtime tools.
- Microsoft Edge WebView2 runtime support.
- Optional tools such as yt-dlp where configured locally.

### Run the Desktop App

Use the packaged desktop shell or installed shortcut for normal operation. The shell connects to the local backend and opens the operator console.

### Run the Backend at Startup

The repository includes Windows startup helpers:

```powershell
.\InstallRadioTEDUMachineStartupTask.ps1
```

Run this from an elevated PowerShell session when installing the machine startup task. The task starts the backend without requiring an interactive user login and also launches the playout guard.

## Operator Workflow

1. Configure station outputs, stream mounts, and Icecast credentials.
2. Import or scan station libraries.
3. Build or let the worker maintain station queues.
4. Monitor station health from the Broadcast Wall operator console.
5. Use Library Operations for local metadata repair, trimming, preview, and playlist management.
6. Keep the public lobby and now-playing endpoints available for station listeners and external displays.

## Public API and Album Art

The public station API exposes listener-facing station data:

```text
GET /api/public/stations
GET /api/public/tracks/{track_id}/cover
```

Now-playing payloads include title, artist, album, duration, stream URLs, and cover art URLs when available. Album art can come from:

- Embedded artwork in the media file.
- `cover`, `folder`, `front`, or `album` image files next to the track.
- The app fallback icon.

Icecast metadata is updated through the standard Icecast `song` field and is formatted as:

```text
Artist - Song Name
```

Cover art is exposed through Broadcast Wall's public metadata and cover endpoints rather than being injected into the Icecast `song` field.

## Development and Verification

Run the regression suite from the repository root:

```powershell
python -m unittest discover -s tests
```

Useful targeted checks:

```powershell
python -m py_compile _internal\app\main.py _internal\app\api\tracks.py
python -m unittest tests.test_playout_hardening tests.test_runtime_registry
node --check _internal\app\static\js\app.js
```

Before committing production-facing changes, check:

- Station runtime health remains green.
- Existing queues and playout state are preserved.
- No live databases, logs, secrets, or generated metadata job files are staged.
- Any metadata operation that touches many tracks is tested in a bounded batch first.

## Git and Large Files

This repository contains packaged runtime files and large binaries. Git LFS is used for files that exceed normal GitHub size limits. Keep local runtime state out of version control and commit only source, packaged application assets, tests, scripts, and intentional documentation changes.

## Operational Notes

- Do not run destructive cleanup against live media directories.
- Do not restart the privileged backend during active programming unless the operator expects a brief interruption.
- Treat all-station metadata jobs as maintenance work, not a casual background task.
- For emergency recovery, verify `/api/public/stations`, `/api/health`, Icecast branch health, worker loop status, and playout guard logs before changing station state.

## License and Ownership

RadioTEDU Broadcast Wall is maintained for RadioTEDU broadcast operations. Confirm licensing, distribution rights, and credential-handling requirements before publishing packaged builds or deploying outside the intended RadioTEDU environment.
