<p align="center">
  <img src="docs/assets/radiotedu-onair-logo.png" width="180" alt="RadioTEDU OnAir">
</p>

<h1 align="center">RadioTEDU OnAir</h1>

<p align="center">
  Deterministic, operator-controlled radio automation for Windows.
</p>

<p align="center">
  <img src="docs/assets/radiotedu-logo-card.png" width="260" alt="RadioTEDU">
</p>

RadioTEDU OnAir is the standalone product path for deterministic Windows radio automation. It is the single supported product path in this repository. It combines the FastAPI playout engine, RadioTEDU operator wall, embedded desktop shell, tray agent, Icecast/local output, queue, jingles, optional AI host, emergency browser-audio takeover, and real-time forecast in one installable product. It replaces the older RadioTEDU Broadcast Wall while preserving compatible data through an explicit, reviewable migration. The separate rtAI application is not built, packaged, or installed from this checkout.

The product is radio- and genre-agnostic. Every station has an independent identity, Icecast mount, managed library, jingle policy, queue, output profile, and persistent start/stop policy. A live source connection is verified only after encoder/output health and listener bytes are observed; other stations remain stopped unless an operator explicitly starts them.

![RadioTEDU OnAir live operator console on the isolated Lo-Fi mount](docs/assets/onair-console-verified.png)

| Operating principle | Guarantee |
|---|---|
| Operator authority | AI, background guards, and unattended clients cannot start or stop a station when restart authorization is disabled. |
| Deterministic stop | Two-step Stop preserves queue order and restarts the interrupted item from its beginning. |
| Independent stations | Mounts, credentials, libraries, queues, jingles, and AI settings remain station-scoped. |
| Emergency takeover | Any audible HTTP/HTTPS browser page can temporarily replace program audio; Stop restores the saved mix. |
| Verifiable changes | Mutations are read back from the backend before the wall reports success. |

## Deterministic Operator Wall

The desktop app opens the RadioTEDU operator wall automatically. In a browser, open `/app`
after the backend starts. A task-based menu separates **On Air**, **Media**,
**Automation**, **Emergency**, **Services**, **Settings**, and **Diagnostics**;
the active workspace is remembered. The wall turns every routine operation
into a visible, verified workflow:

- Start and stop any selected station with read-back verification that persists across restarts.
- Create, rename, configure, test, and delete stations.
- Edit and test each station's Icecast destination and mount.
- Set an exact managed music folder so autoplay cannot drift into another station's library.
- Select folders with the native operating-system picker; no path copying or command line is required.
- Search the active library, add tracks to the queue, remove them, and reorder them.
- Upload jingles or maintain an exact station-specific jingle folder.
- Enable station-isolated automatic jingles after an operator-selected number of completed songs. The default is every 2 songs, but 1–100 and ordered/random selection are adjustable; the current song always finishes first.
- Stop a stream without clearing, advancing, or reordering its playlist. The interrupted item stays in place and restarts from its beginning when the operator resumes.
- Fresh stations remain stopped until an operator presses Start. Restart autostart is a separate, explicit setting and AI can never start, stop, or veto the broadcast.
- Preview an official TRT or custom approved emergency source, then broadcast only the explicitly shared tab audio.
- See the current song's remaining time plus forecasted start/end times for upcoming songs.
- Edit Icecast credentials, HE-AAC v1 codec (96/192 kbps), output gain, and local monitor device.
- Configure, enable, disable, test, and repair the optional AI host and TTS runtime.
- Run the installation self-check and change the signed-in operator password.

Mutation requests are followed by authoritative read-back checks. Safe/idempotent actions retry short transport failures, and a dropped response is reported as success only if the requested state is independently observed afterward. See `docs/DETERMINISTIC_OPERATOR_GUIDE.md` for the operator workflow and recovery rules.

## What Lives Where

- `app`: FastAPI app, runtime engine, repositories, setup/preflight APIs, and operator UI bridge
- `desktop`: Windows shell and tray agent projects
- `installer`: Inno Setup smart installer and WebView2 prerequisite bootstrapper
- `tests`: unit and integration coverage
- `scripts`: maintenance and release helpers such as `import_legacy_data.py` and `release_bundle.py`
- `data`: local development runtime state, media, tools, and working database
- `docs/plans`: design and implementation notes
- `docs/MUSIC_USAGE_COMPLIANCE.md`: append-only repertoire records, daily CSV export, and monthly close workflow

## Daily Commands

- Install the verified dependency graph: `python -m pip install --only-binary=:all: -r requirements.lock`
- Run locally: `uvicorn app.main:app --reload`
- Run the local launcher: `python run_cleanroom.py`
- Verify foundation changes: `python -m pytest tests -q`
- Run all tests: `python -m pytest`
- Import legacy data: `python scripts/import_legacy_data.py`
- Build packaged backend bundle: `powershell -ExecutionPolicy Bypass -File .\build_backend_onefile.ps1`
- Package portable diagnostic release: `powershell -ExecutionPolicy Bypass -File .\package_portable_release.ps1`
- Build Windows desktop installer: `powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1`
- Smoke-test desktop release artifacts: `powershell -ExecutionPolicy Bypass -File .\smoke_test_desktop_bundle.ps1`

Local development stays simple: `uvicorn app.main:app --reload` or `python run_cleanroom.py`.

`requirements.txt` is the human-maintained direct-dependency input.
`requirements.lock` is the exact Windows test/runtime graph used by CI,
installer builds, and backend packaging. Dependency updates are intentional
release changes: update both files, then rerun the complete Python, browser
contract, JavaScript syntax, desktop, packaging, and smoke-test gates.

## License and publication status

The application is currently a proprietary TED University distribution under
`LICENSE.md`; only the installer-authoring source has the separate MIT license
in `installer/LICENSE.md`. The repository must not be described or published
as an open-source application until TED University selects an application
license and completes the legal, trademark, dependency, clean-machine, and
broadcast gates in `docs/OPEN_SOURCE_RELEASE_CHECKLIST.md`.

## For Stations And Operators

RadioTEDU OnAir is designed for non-technical station operators, including RadioTEDU. The installed app opens directly on the RadioTEDU operator wall and runs the backend in the background through the tray agent or Windows service.

First launch reaches the ready-to-stream state only after:

- Backend health is confirmed.
- FFmpeg, ffplay, ffprobe, yt-dlp, and desktop prerequisites are available or remediated.
- A usable station exists with a valid mount.
- Every enabled output is configured and verified; the local monitor remains optional.
- The operator enters the Icecast server URL, mount, source username, source password, and HE-AAC v1 codec profile.
- RadioTEDU sends a short silent Icecast test stream to prove the URL, source credentials, mount, and selected codec are accepted.
- Optional AI Host and TTS runtime readiness are verified when AI is enabled.
- A RadioTEDU deployment certificate is issued for the exact verified configuration.

The setup workflow includes friendly progress states, actionable errors, retry buttons, and a final completion gate. It persists setup state and removes the deployment certificate until a changed output configuration is tested again.

For a fresh installation, read the generated `initial-admin-password.txt` from
the OnAir data directory, sign in as `admin` only on the loopback/local
desktop, and immediately use **Account → Change operator password**. The app
does not ship a fixed administrator password. When `JWT_SECRET_KEY` is not
supplied, it stores a private random signing key under the per-user OnAir
configuration directory instead of using a published default. For network
deployments, set a separate random `JWT_SECRET_KEY`, HTTPS, explicit
`CORS_ORIGINS`, and a trusted reverse proxy.

### Independent station profiles

Use **Exact replacement** for each station library:

| Station | Mount | Managed music | Defaults |
|---|---|---|---|
| Pop | `/radio` | `D:\Radio\Music\Pop` | genre `Pop` |
| Community Rock | `/rock` | `D:\Radio\Music\Rock` | genre `Rock`, language `en` |

Exact replacement deactivates only the selected station's out-of-profile tracks, removes stale pending references, leaves a currently playing song to finish, and refills that station's queue from the verified folder.

### Emergency broadcast

The **Emergency** workspace is independent of `/lofi`. Its built-in presets
open the official TRT Radyo 1, TRT FM, or TRT Radyo Haber page; an operator may
instead enter any approved HTTP/HTTPS public-service source. **Open and
preview** never changes the broadcast. The red takeover control requires two
clicks, then the browser requires the operator to choose the opened tab and
enable tab-audio sharing. OnAir verifies incoming audio frames before declaring
the takeover live. Stop, tab closure, loss of the shared audio track, or a
failed start restores the saved playlist mix.

### RadioTEDU service control

**Services** is the local control plane for the related
RadioTEDU systems:

- Ollama: detected local model runtime, installed-model health, guarded runtime control, and fixed model installation
- RadioTEDU AI Radio: Shared AI and the independent EN/FR broadcast supervisor
- RadioTEDU Voting: local voting agent and web backend
- RadioTEDU Juke: local media agent and web backend

Each component has explicit enable and startup switches, an absolute source
folder, a protected configuration-folder or `.env` path, health endpoints, and
an optional database-backup folder. Every filesystem field has a native Browse
button, so the complete setup can be performed without typing paths. The wall
shows source readiness, Git commit
and local-change state, managed PID state, endpoint latency, and sanitized
health signals. Operators can check, start, stop, and restart each fixed
component entirely with the mouse. Clean Git repositories can be updated only
by a guarded fast-forward; database maintenance always creates its required
backup first. Start, stop, restart, repository updates, model installation, and
database updates must be confirmed by a second click within 20 seconds.

The control plane never accepts an arbitrary command and never reads a secret
into browser settings. Credentials remain in the protected external
configuration supplied by each authoritative RadioTEDU repository. Plain HTTP
health checks are restricted to loopback; external health endpoints require
HTTPS. Only one managed service may own a stream mount at a time, and the AI
broadcast supervisor cannot start until Shared AI reports healthy.

Database updates are offline maintenance operations. The managed service must
be stopped first. PostgreSQL services require `pg_dump`, create a timestamped
custom-format backup, and then run only their repository-defined migration
scripts. The AI supervisor backs up each station-local SQLite database, applies
its numbered migration ledger, and rescans the station music catalog. A failed
backup blocks migration. Successful maintenance is recorded outside the
database and displayed with database type, readiness, completion time, backup
count, and migration count; no credential or database URL is stored there.

For an authorized RadioTEDU broadcast computer,
`tools/provision_rtmd_integrations.py` imports the private machine handoff into
ACL-protected files under `C:\ProgramData\RadioTEDU`, pins the six repository
service cards to the checked-out repositories, exposes the detected local
Ollama runtime as a seventh card, and leaves every autostart switch off. The
script prints only a count and never prints credentials. Machine-specific
`.env` files, the private handoff, databases, media, and generated secrets must
never be added to Git. Operators can review paths and health from the dashboard
after provisioning; secret values deliberately remain invisible.

## Windows Installer

- The installer source is open source under `installer/LICENSE.md`. The app payloads and trademarks remain under their own licenses.
- Build the desktop bundle and installer with `powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1`.
- If `ISCC.exe` is not on `PATH`, pass `-InnoSetupCompiler C:\Path\To\ISCC.exe` or set `INNO_SETUP_COMPILER`.
- Install from `release\setup\RadioTEDU-OnAir-Setup-<version>.exe`, the generated setup.exe-compatible installer.
- Choose `Current user` for a per-user install or `All users` for a machine-wide install when the setup wizard asks.
- The installer verifies WebView2 Runtime and installs it only when missing. The official desktop bundle is self-contained; optional .NET 8 Desktop Runtime and Ollama installation tasks are unchecked by default.
- Optional offline payloads can be placed under `release\prerequisites` before building: `OllamaSetup.exe`, `ollama.exe`, `python-embed-amd64.zip`, `qwen3-tts-voice-design.zip`, `models\*`, or `runtimes\*`.
- A desktop shortcut is enabled by default for the TEDU edition. Start Menu shortcut and startup/tray behavior remain supported.
- The installer launches `RadioTEDU-OnAir-Agent.exe`, which opens the branded desktop app instead of a browser tab.
- The backend is installed as a packaged bundle and launched hidden by the tray agent, so no visible CMD window is shown during normal use.
- Clicking the window close button sends RadioTEDU OnAir to the system tray instead of exiting.
- Use the tray menu to open the panel, restart an Agent-owned backend, or stop the app completely after confirmation. Service-owned deployments use **Diagnostics → Reload backend safely**, which preserves queue order and verifies a new backend process before reporting success.

Run `powershell -ExecutionPolicy Bypass -File .\smoke_test_desktop_bundle.ps1` after `build_setup.ps1` to validate the exact installer path recorded in `release\setup\last_setup_path.txt` and the exact backend artifact recorded in `last_build_path.txt`, then smoke-test the packaged backend on an automatically allocated free loopback port with `CLEANROOM_OPEN_PANEL=0`.

## Release Artifacts

GitHub Releases should publish these branded artifacts:

- `release\setup\RadioTEDU-OnAir-Setup-<version>.exe`
- `dist\backend\RadioTEDU-OnAir-Backend.exe`
- `dist\desktop\RadioTEDU-OnAir-Agent.exe`
- `dist\desktop\shell\RadioTEDU-OnAir.exe`
- Optional portable diagnostic folder from `package_portable_release.ps1`
- Smoke validation notes from `smoke_test_desktop_bundle.ps1`
- Exact validation evidence in `docs/TEST_REPORT.md`

Operator documentation:

- `docs/DETERMINISTIC_OPERATOR_GUIDE.md`
- `docs/TROUBLESHOOTING.md`
- `docs/CONFIGURATION_REFERENCE.md`
- `docs/TEST_REPORT.md`

Commissioning and read-only media verification:

```powershell
python tools/audit_wall_migration.py --source-db C:\path\to\wall.db --target-db C:\path\to\onair.db
python tools/validate_active_media.py --db C:\path\to\onair.db --report C:\path\to\active-media-report.json --workers 4
```

The migration audit checks stations, track groups, missing active files, and
output/mount settings. The media validator opens every unique active audio file
with the managed media probe and writes a machine-readable report without
changing the station database.

Screenshots should be attached under the release notes when available:

- First-run setup wizard
- Main On Air control surface
- Settings output configuration
- Tray menu

## Access And Auth

- Open `/` for the public station lobby and `/app` for the authenticated control surface.
- Open `/login.html` to sign in with a valid user account.
- `POST /api/auth/login` returns bearer access and refresh tokens.
- `POST /api/auth/refresh` rotates refresh tokens and issues a new access token.
- `GET /api/auth/me` returns the active session user.
- The backend serves JWT-protected APIs for authenticated frontend and operator traffic.
- Station selections flow through login: lobby station cards hand off to `/login.html?station_id=...&next=/app?station_id=...`, and the authenticated shell uses that station context when it opens.
- The authenticated shell shows a 15-minute inactivity timeout warning during the final minute and signs the user out if activity does not resume.

## Authorization Model

RadioTEDU OnAir uses layered RBAC with legacy-role compatibility during the migration.

- Legacy `admin`, `dj`, `producer`, and `viewer` accounts are mapped to seeded system role templates.
- Users can hold multiple role templates, and effective permissions are the union of every assigned template.
- Show access is managed separately from global role templates.
- Show-specific capabilities include `show.broadcast`, `show.queue_edit`, `show.jingle_manage`, `show.break_control`, and `show.end`.
- Sensitive actions such as station creation, role management, setup completion, and program access require explicit permissions.

## Live Mic Workflow

- Live mic control is available only to `admin` and `dj` accounts in the protected On Air program workspace.
- Studios are station-scoped. A station can have multiple studios, but only one studio per station may be on air at a time.
- Mic access follows the active DJ of the on-air studio. Producers may join and chat, but they cannot take the mic.
- The browser must grant microphone permission before capture starts.
- Browser capture uses `getUserMedia()` plus `MediaRecorder` and sends authenticated mic chunks over `/ws?token=<access-token>&station_id=<station-id>`.
- The server decodes those chunks with FFmpeg, mixes them into the current station runtime, and writes the result to Icecast and local outputs.
- The program panel supports `push-to-talk` and `always-on` transmission modes.
- Program settings are backed by `PUT /api/audio/live/settings`.
- When `aiortc` is installed and `WEBRTC_ENABLED=true`, the browser attempts WebRTC for lower-latency audio transport. If WebRTC negotiation fails, the browser falls back to the WebSocket and MediaRecorder path automatically.
- Configure TURN via `WEBRTC_TURN_URL`, `WEBRTC_TURN_USERNAME`, and `WEBRTC_TURN_CREDENTIAL` for WAN deployments.

## Phase 4A Deployment

- For reverse-proxy deployment, place the app behind HTTPS and let the browser reach `/ws` over WSS.
- The recommended proxy path is Caddy or an equivalent HTTPS terminator. Keep the app itself on HTTP and terminate TLS at the proxy.
- Set `PUBLIC_BASE_URL` when you know the external origin, for example `https://radio.example.com`.
- Set `CORS_ORIGINS` to the public origins that should be allowed, for example `https://radio.example.com,https://ops.example.com`.
- Set `TRUST_PROXY_HEADERS=true` when the app is behind a trusted reverse proxy that overwrites `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-For` for login rate limiting.
- Leave `SECURITY_HEADERS_ENABLED` on unless you are explicitly debugging a browser quirk.

### WebRTC Configuration

| Variable | Default | Description |
|---|---|---|
| `WEBRTC_ENABLED` | `true` | Master switch for WebRTC audio transport |
| `WEBRTC_STUN_URL` | `stun:stun.l.google.com:19302` | STUN server URL |
| `WEBRTC_TURN_URL` | empty | TURN server URL for NAT traversal |
| `WEBRTC_TURN_USERNAME` | empty | TURN authentication username |
| `WEBRTC_TURN_CREDENTIAL` | empty | TURN authentication credential |

### HTTPS/WSS Smoke Checklist

- Open the panel over `https://<public-host>/`.
- Confirm login still works and the browser receives `X-Cleanroom-Public-Origin`.
- Confirm the websocket connects to `wss://<public-host>/ws?token=...&station_id=...`.
- Confirm live mic, studio chat, queue updates, On Air, and Playlists still work through the proxy.
- Confirm `/api/*` traffic is not being cached by the service worker.

## Runtime Notes

Icecast, local monitor output, On Air, and Playlists flow through the same backend. The import flow keeps `music -> music` semantics, supports `hard cut` fallback behavior where needed, and the queue API now persists to SQLite so worker recovery and UI polling share the same source of truth.

## Play history and nightly backup

Every completed music or jingle queue item is written to the append-only,
hash-chained `music_usage_log`. A background exporter keeps
`%USERPROFILE%\Desktop\RadioTEDU Play History` current with daily and total
all-radio history/count CSVs, dated `daily\` files, preserved legacy reports,
and `RadioTEDU-play-history-manifest.json`. The five-minute Windows export task
and nightly `RadioTEDU-OnAir-PlayHistory-GitHub` task do not own or interrupt
audio. The nightly task pushes only these reports to the private
[`radiotedu/RadioTEDU-OnAir-Play-History`](https://github.com/radiotedu/RadioTEDU-OnAir-Play-History)
repository; credentials, databases, media, and stream processes remain local.
See [docs/PLAY_HISTORY_BACKUP.md](docs/PLAY_HISTORY_BACKUP.md) for the Turkish
operator procedure.

On first launch, the backend bootstraps managed copies of yt-dlp, FFmpeg, ffplay, ffprobe, and packaged AI runtime payloads into `%LOCALAPPDATA%/RadioTEDU OnAir/tools`. Startup also re-probes any active local tracks that still have `duration=0` so imported libraries recover from stale `00:00` metadata as soon as ffprobe is available. The system is designed around Icecast and local output together, with the Windows installer shipping a packaged backend bundle plus desktop shell/tray applications from RadioTEDU Technologies.
