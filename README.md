<p align="center">
  <img src="docs/assets/radiotedu-onair-logo.png" width="190" alt="RadioTEDU OnAir">
</p>

<h1 align="center">RadioTEDU OnAir</h1>

<p align="center">
  Unattended, deterministic multi-channel broadcasting for RadioTEDU.
</p>

<p align="center">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-boot%20service-0078D4?logo=windows">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-control%20plane-009688?logo=fastapi&logoColor=white">
  <img alt="Streaming" src="https://img.shields.io/badge/streams-Opus%20%2B%20FLAC-EA4C89">
</p>

RadioTEDU OnAir is the broadcast engine, operator wall, desktop shell, and Windows service used to keep RadioTEDU's music stations on air. Each station has an independent library, queue, playout worker, Icecast connection, output fan-out, and recovery loop. A failed mount reconnects independently; it does not restart the programme or interrupt sibling stations.

![RadioTEDU OnAir operator console](docs/assets/onair-console-verified.png)

## Public stream contract

The unsuffixed mount is always the normal stream. Low-bandwidth streams use `-low`. Lossless output is deliberately limited to Classical and Cazz.

| Radio | Low — Opus 32 kbps | Normal — Opus 192 kbps | Lossless — FLAC |
|---|---|---|---|
| RadioTEDU | `/radio-low` | `/radio` | — |
| Lo-Fi | `/lofi-low` | `/lofi` | — |
| Classical | `/classic-low` | `/classic` | `/classic-flac` |
| Cazz | `/cazz-low` | `/cazz` | `/cazz-flac` |
| Rock | `/rock-low` | `/rock` | — |
| Energetic | `/energize-low` | `/energize` | — |

This PC owns 14 local music sources: six normal, six low, and two FLAC. The externally operated `/en` and `/fr` AI sources bring the complete RadioTEDU plan to 16 mounts. Retired `-normal`, `-high`, and non-approved FLAC branches are removed during commissioning.

## Designed to survive a reboot

The stream is not tied to the desktop window. `RadioTEDU.OnAir.Supervisor` is a delayed-auto Windows service with failure recovery. At machine startup it launches the backend, restores the six operator-authorized station workers, and starts the actual Icecast source pipelines—even if nobody signs in or opens the app.

The commissioning command enables `broadcast_autostart_enabled` for the six music stations, enforces Opus 192 on their primary mounts, writes the approved eight additional outputs, makes an integrity-checked database backup, and preserves protected Icecast credentials.

```powershell
python .\tools\commission_quality_outputs.py `
  --backup-root "C:\ProgramData\RadioTEDU\OnAir\backups\quality-commission"
```

Install or repair the machine service from an elevated PowerShell prompt:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\Install-RadioTEDU-OneShot.ps1
```

## What is included

| Area | Responsibility |
|---|---|
| `app/` | FastAPI control plane, playout engine, persistence, stream fan-out, operator UI |
| `desktop/` | Windows desktop shell and tray integration |
| `tools/` | Service installation, commissioning, watchdogs, recovery, verification |
| `installer/` | Windows installer authoring and prerequisites |
| `tests/` | Python, JavaScript, integration, installer, and reliability contracts |
| `docs/` | Architecture, operations, recovery, and operator guidance |

## Development

Use Python 3.12 on Windows. Runtime state, databases, credentials, media, generated packages, and local tool bundles do not belong in Git.

```powershell
python -m pip install --only-binary=:all: -r requirements.lock
python run_cleanroom.py
```

Open `http://127.0.0.1:18110/app` for the operator wall.

Run the core verification gates:

```powershell
python -m pytest -q
node --test tests\js\*.test.cjs
node --check app\static\onair\app.js
```

Build the packaged backend and desktop installer:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_backend_onefile.ps1
powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1
```

## Operating guarantees

- One programme timeline fans out to every quality branch; variants do not create duplicate repertoire events.
- Primary mount names, source usernames, passwords, host settings, devices, gain, and protocol remain protected during quality commissioning.
- Quality-output settings never persist copies of source credentials.
- A source is reported healthy only after verified delivery, not merely because a TCP connection opened.
- AI cannot veto deterministic music continuity or acquire authority over start/stop state.
- Every material configuration mutation is read back before the operator wall reports success.

See [Quality-output architecture](docs/QUALITY_OUTPUTS_ARCHITECTURE.md), [16-mount operations](docs/16_MOUNT_STREAM_OPERATIONS.md), and [Deterministic operator guide](docs/DETERMINISTIC_OPERATOR_GUIDE.md).

## Security and publication boundary

The repository contains source and reviewable configuration only. `.env` files, SQLite databases, JWT material, credential stores, certificates, keys, logs, media libraries, build output, and machine-local service data are excluded. Live secrets remain in the Windows-protected RadioTEDU data root.

## License

RadioTEDU OnAir is distributed under the terms in [LICENSE.md](LICENSE.md). Copyright © TED University / RadioTEDU.
