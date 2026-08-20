# RadioTEDU OnAir Configuration Reference

RadioTEDU OnAir is configured primarily through the authenticated dashboard.
Environment variables are intended for deployment, path isolation, security,
and diagnostics. Station credentials are stored in the protected credential
vault and are not returned in plaintext by the API.

## Configuration precedence

1. Explicit environment variable.
2. Saved system or station setting.
3. Product default.

Environment variables are read when the backend starts. Restart the OnAir
agent/backend after changing them.

## Windows storage layout

| Purpose | Installed default |
|---|---|
| Binaries | `%ProgramFiles%\RadioTEDU\OnAir` |
| Shared mutable data | `%ProgramData%\RadioTEDU\OnAir` |
| Database | `%ProgramData%\RadioTEDU\OnAir\cleanroom.db` |
| Managed tools/state | `%ProgramData%\RadioTEDU\OnAir\tools` and `state` |
| Managed media | `%ProgramData%\RadioTEDU\OnAir\Media` |
| Per-user config/secrets | `%LOCALAPPDATA%\RadioTEDU\OnAir` |
| WebView user data | `%LOCALAPPDATA%\RadioTEDU\OnAir\EBWebView` |

The legacy Broadcast Wall paths are never adopted automatically. Use the
explicit migration command and review its dry-run report.

## Server and path variables

| Variable | Default | Meaning |
|---|---|---|
| `CLEANROOM_HOST` | `127.0.0.1` | Backend bind address. |
| `CLEANROOM_PORT` | `8100` | Backend HTTP port. |
| `CLEANROOM_OPEN_PANEL` | enabled | Set `0` to suppress automatic panel opening. |
| `CLEANROOM_DATA_ROOT` | ProgramData OnAir root when packaged | Override all shared mutable data for isolation/tests. |
| `CLEANROOM_USER_CONFIG_ROOT` | LocalAppData OnAir root | Override per-user configuration and secrets. |
| `CLEANROOM_DB_PATH` | `<data-root>\cleanroom.db` | Explicit database path. |
| `CLEANROOM_TOOLS_DIR` | packaged/managed tools directory | FFmpeg/FFprobe/FFplay/yt-dlp root. |
| `CLEANROOM_DEPENDENCY_STATE_FILE` | `<data-root>\state\dependency-bootstrap.json` | Dependency bootstrap state. |
| `CLEANROOM_CREDENTIAL_STORE_FILE` | protected user configuration location | Credential-vault file override. |
| `CLEANROOM_JWT_SECRET_FILE` | `<user-config>\secrets\jwt-signing.key` | JWT signing-key file override. |
| `CLEANROOM_YTDLP_URL` | official yt-dlp latest executable URL | Dependency source override. |
| `CLEANROOM_STATION_ID` | selected/default station | Initial station selection for supported launch paths. |

Use path overrides only when the entire deployment intentionally uses the same
isolated root. Partial overrides can make diagnostics harder.

## Authentication and HTTP security

| Variable | Default | Meaning |
|---|---|---|
| `JWT_SECRET_KEY` | generated persistent random key | Explicit JWT signing secret. Use a unique random value for network deployments. |
| `CLEANROOM_INITIAL_ADMIN_PASSWORD` | generated random password file | Optional provisioning override. Do not ship a public fixed value. |
| `AUTH_RATE_LIMIT_MAX_REQUESTS` | `12` | Authentication requests allowed per window. |
| `AUTH_RATE_LIMIT_WINDOW_SECONDS` | `60` | Authentication rate-limit window. |
| `CORS_ORIGINS` | `http://localhost:8100` | Comma-separated allowed origins. |
| `PUBLIC_BASE_URL` | empty | Canonical external base URL when deployed behind a proxy. |
| `TRUST_PROXY_HEADERS` | `false` | Trust forwarded client/protocol headers only behind a controlled proxy. |
| `SECURITY_HEADERS_ENABLED` | `true` | Enable response security headers. |
| `MAX_UPLOAD_BYTES` | `536870912` | Maximum upload size (512 MiB). |

For non-loopback access, configure HTTPS/WSS, a trusted reverse proxy, an
explicit `PUBLIC_BASE_URL`, narrow `CORS_ORIGINS`, and a private JWT secret.

## WebRTC microphone variables

| Variable | Default | Meaning |
|---|---|---|
| `WEBRTC_ENABLED` | enabled when the runtime is installed | Enables WebRTC microphone/signaling support. |
| `WEBRTC_STUN_URL` | `stun:stun.l.google.com:19302` | STUN server. |
| `WEBRTC_TURN_URL` | empty | TURN URL required for many WAN/NAT deployments. |
| `WEBRTC_TURN_USERNAME` | empty | TURN username. |
| `WEBRTC_TURN_CREDENTIAL` | empty | TURN credential; treat as a secret. |

Local desktop microphone use does not grant permission to broadcast. The
operator must select a device, verify the meter, and explicitly take it live.

## Capacity and retention variables

| Variable | Default | Meaning |
|---|---|---|
| `MAX_LOCAL_OUTPUTS` | `4` | Maximum simultaneous local output devices. |
| `MAX_OPERATION_LOG_ROWS` | `50000` | Retained operation log rows. |
| `MAX_EVENT_ROWS` | `20000` | Retained event rows. |
| `RADIOTEDU_SCHEMA_BACKUP_RETENTION` | `8` | Verified pre-migration SQLite backups retained per database; values are clamped to `1`–`64`. |

Before an existing SQLite database receives a pending schema/bootstrap change,
OnAir creates and verifies an online backup under `schema-backups` beside the
database and records it in `schema-migration-backups.json`. A backup, integrity,
or ledger failure blocks the migration before schema writes occur. Fresh or
already-current databases do not create redundant backups.

## Optional AI variables

| Variable | Default | Meaning |
|---|---|---|
| `AI_PRELOAD_MODELS` | `true` | Warm configured local AI models on startup. |
| `QWEN_TTS_REQUEST_TIMEOUT_SECONDS` | `86400` | Upper bound for a local Qwen TTS request. |
| `CLEANROOM_SKIP_STARTUP_AI` | `false` | Diagnostic/smoke flag that suppresses startup AI warming. |

AI failure must not disable core playout. Configure providers, voices, and
station-specific AI behavior in the dashboard.

## Diagnostic and smoke-test flags

| Variable | Default | Meaning |
|---|---|---|
| `CLEANROOM_DISABLE_LIBRARY_WATCHER` | `false` | Disable automatic managed-folder polling. |
| `CLEANROOM_SKIP_WORKER_AUTOSTART` | `false` | Do not automatically start station worker loops. |
| `CLEANROOM_SKIP_ICECAST_METADATA` | `false` | Suppress Icecast metadata updates. |

These flags are for isolated tests and fault diagnosis. Leaving them enabled in
production can make the station appear healthy while automation is inactive.

## Dashboard-managed station settings

Operator-owned broadcast controls:

| Setting | Default | Meaning |
|---|---:|---|
| `broadcast_autostart_enabled` | `false` | Resume this station after application restart only when the operator explicitly enables it |
| `sweeper_enabled` | `false` | Enable automatic jingles for this station |
| `sweeper_interval` | `2` | Completed songs between automatic jingles; dashboard accepts 1–100 |
| `sweeper_interval_unit` | `tracks` | Deterministic song-count cadence used by the dashboard |
| `sweeper_mode` | `ordered` | `ordered` library rotation or `random` selection |

The operator-stop action preserves queue count and order. Any interrupted
queue, advertisement, or scheduled item is returned to `pending`; a host item
remains queued. Resume restarts the interrupted item from its beginning.

AI settings never confer permission to start, stop, or autostart a station.
When AI is unavailable, operator-authorized playout continues with normal music
continuity.

Each station has independent:

- Identity, active/inactive state, and operator access.
- Icecast host, port, mount, source user, protected password, codec, gain, and
  enablement.
- Local output device and local-output enablement.
- Managed media folder, scan mode, and library.
- Playlists, queue, rotation, jingles, advertisements, schedules, shows, and
  break rules.
- Microphone/device/gain/ducking controls.
- Metadata delivery rules and retry status.
- Voting and Study adapter configuration.
- Optional AI provider, voice, cache, and readiness state.

Changing one station must not silently alter another station's output or media
profile.

## Operator workspaces and optional services

The dashboard remembers the last selected **On Air**, **Media**,
**Automation**, **Emergency**, **Services**, **Settings**, or **Diagnostics**
workspace. Station registration and output credentials live only in Settings;
daily start/stop and timeline state live only in On Air.

The Services workspace exposes a fixed allowlist: Ollama, Shared AI, the AI
broadcast supervisor, Voting agent/backend, and Juke media agent/backend.
Ollama uses the loopback `http://127.0.0.1:11434/api/tags` endpoint and accepts
only validated model names for its fixed `ollama pull` action. Repository
updates require a stopped service, clean worktree, configured upstream, and
fast-forward merge. Database updates require a stopped service and successful
backup before a fixed migration command is allowed.

## Codec profiles

The output configuration supports the packaged profiles exposed by the
dashboard, including AAC, Opus, MP3, Ogg/FLAC where accepted by the destination.
Use **Test stream destination** after changing codec, mount, or credentials.

## Build and release commands

Official release builds require self-contained desktop publishing. The setup
wizard leaves both optional runtime tasks—.NET 8 Desktop Runtime and
Ollama—unchecked by default. Enable one only when an operator has explicitly
chosen it; WebView2 is checked automatically because the native desktop shell
uses it.

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1 -Version 1.0.0
powershell -ExecutionPolicy Bypass -File .\smoke_test_desktop_bundle.ps1
```

The build records the exact backend and installer paths and generates a
SHA-256 sidecar. Public binary distribution additionally requires an
organizational Authenticode signature.
