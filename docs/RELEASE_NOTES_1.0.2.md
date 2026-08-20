# RadioTEDU OnAir 1.0.2 release notes

## Current artifact gate

The RadioTEDU 1.0.2 release is built from the locked Python/.NET dependency graph and uses product-isolated build, desktop, and installer paths. This checkout contains only the separate RadioTEDU product; the unrelated rtAI application is neither built nor installed from it.

The build chain now stops repository-local backend/worker processes before cleaning the canonical backend directory. If an external scanner still forces a timestamp fallback, desktop packaging promotes the complete bundle (`_internal`, managed tools, provenance, and executable) instead of mixing a new executable with stale runtime files.

FFmpeg, FFprobe, and FFplay are staged from the same operator-pinned FFmpeg distribution and recorded in backend provenance. A runtime-downloaded `latest` yt-dlp binary is never silently promoted into the reproducible installer.

| Artifact | Evidence |
| --- | --- |
| Backend | `dist\backend\RadioTEDU-OnAir-Backend.exe`, 10,409,940 bytes, SHA-256 `A0ABE71FD8A13C932910DFE54D8F6190A08D3F1163DD20778CECBE9EED2AF952` (clean source commit `ed80e92`) |
| Installer | `release\setup\RadioTEDU-OnAir-Setup-1.0.2.exe`, 484,896,969 bytes, SHA-256 `187757B5EA8E2981D9418EDD59E2BF087FE0E41016FF90524EBEDD7D4B2E64A5` |
| Release scope | `scripts\validate_radiotedu_release.py` validated 2,615 staged files, including product executables, managed FFmpeg/FFplay/ffprobe copies, and scoped .NET crash helpers. |
| Desktop smoke | `smoke_test_desktop_bundle.ps1` passed on disposable loopback port 18103. Shell and supervisor icons extracted with 164 RadioTEDU-red pixels at 32x32. |
| Backend smoke | `smoke_test_backend_onefile.ps1` passed isolated startup, login, SQLite safety checks, UI asset delivery, and representative station/API routes. Readiness remained intentionally unavailable because this volume is below the configured 3% disk reserve gate. |
| Real stream fixture | `tests\integration\test_radiotedu_real_stream.py` passed with the actual FFmpeg encoder and local SHOUTcast v1 source fixture: authenticated handshake, ICY headers, nonzero encoded bytes, and live health counters. |
| Soak facility | `tools\soak_test_onair.py` provides a fail-closed 24-hour default observation runner. It now rejects stopped/untracked playout, unhealthy required outputs, recovery-counter increases, encoder-error increases or resets, and missing optional listener audio bytes. Credentials and encoder URLs remain redacted. |

## Safety and migration status

- Stream destination editing no longer blocks the operator for a synchronous
  60-second public-listener test. An administrator can save and authoritatively
  read back a locally safe destination immediately; the running engine reloads
  it and continues reconnecting while listener verification remains visibly
  `needs_attention`. Unsafe local, credential, mount-conflict, media, and HA
  results remain blocked, and a runtime reload failure still restores the
  previous output.
- The staged migrated instance saved station 1's requested `/classic` mount in
  0.91 seconds, retained the original protected credential reference, and the
  process-isolated worker reported `/classic` on its metadata delivery path.
  Stations 1, 2, and 5 were then authorized for unattended restart; after a
  full backend/worker stop, all three restored worker, program, media-input,
  and FFmpeg-output state without an operator API call.
- A user-level `RadioTEDU OnAir (New)` desktop shortcut starts or reuses only
  the separate port-18110 staged instance and opens its local operator panel.
  It does not modify the legacy port-8100 service or shortcuts.
- The current installed database, media, programs, services, and legacy scheduled tasks remain preserved. No old program or live station was deleted as part of the artifact gate.
- `tools\backup_onair_state.py` now stages snapshots atomically and publishes a manifest only after every selected file and the SQLite online backup pass. It fails closed before creating a staging tree when a protected credential file cannot be read.
- Verified recovery points can now be staged for an offline supervisor restore without overwriting the active database. The admin staging path rechecks the protected point's digest and SQLite integrity; the supervisor retains the current database for rollback and applies the staged copy only before backend startup.
- A separate, repository-local RadioTEDU run was launched from a read-only SQLite online backup at `run\new-program`. It preserved 7 stations, 37,462 tracks, 8 outputs, 4,705 queue rows, and the DPAPI station credential store without touching the installed database. The real 5,822-file and 10,864-file managed folders now reconcile incrementally instead of failing at the former 5,000-file scanner limit; three unreadable station-9 files are reported without deleting source media.
- The final clean `ed80e92` backend was started on loopback port 18110 against that migrated clone. Stations 1, 2, and 5 have process-isolated workers, active track inputs, flowing PCM, and FFmpeg source encoders. One-shot commissioning samples passed for all three, but these samples are not a substitute for the sustained soak gate.
- Packaged startup and SQLite integrity succeeded. Readiness remains deliberately unavailable because the C: volume has about 12.53 GB free but only 1.33% reserve, below the configured 3% production disk gate.
- The first preflight backup attempt reached the existing ACL boundary on the protected voting-agent `.env`. A complete migration backup must be rerun from an elevated operator account; do not weaken the ACL or copy the secret into a user-readable workspace.
- The remote endpoint originally closed source and listener sessions with socket
  error `-10054`; the reversible SHOUTcast trial was unsuccessful and remained
  reverted. After the final Icecast backend restart and requested mount change,
  public listener probes recovered transiently for `/classic` and `/lofi`.
  FFprobe read AAC, 48 kHz, two-channel audio from both mounts, and independent
  encoded samples received 138,653 and 166,521 bytes respectively. Subsequent
  fail-closed commissioning samples again received connection resets, proving
  that the remote service remains intermittent rather than production-stable.
  `/radiotedu4` still returns
  404 to listeners while its source reports 401, so station 5 requires a valid
  server-side source credential or mount authorization. The successful samples
  prove the encoders can reach public listeners during remote recovery windows,
  but the required 24-hour monitored soak cannot start until the upstream
  resets stop.

The soak runner is intentionally not a substitute for commissioning. Run it only after the elevated backup and cloned migration have passed:

```powershell
python tools\soak_test_onair.py `
  --api-base http://127.0.0.1:8100 `
  --password-file C:\ProgramData\RadioTEDU\OnAir\initial-admin-password.txt `
  --stream-url https://<verified-public-listener>/stream `
  --output C:\ProgramData\RadioTEDU\OnAir\Recovery\soak\station-1.jsonl
```

Use `--once` for a commissioning sample. A 24-hour result is valid only when the final JSONL summary reports `passed: true`, every sample has flowing audio bytes, and all reconnects are explained in the operator log.

## Commissioning order

1. From an elevated PowerShell session, create a new Recovery snapshot and verify its manifest and SQLite integrity before any service stop.
2. Run the installer's preflight/migration flow against a cloned database and protected credential vault. Compare station/output/queue/program counts and verify every credential reference decrypts under the service identity.
3. Stop the legacy supervisor only after the clone and rollback point are accepted; install 1.0.2, verify service identity/ACL/WER configuration, and start the new supervisor.
4. Verify each enabled station independently, including fallback behavior, Icecast/SHOUTcast handshake, metadata, byte progress, and public HTTP audio.
5. Keep the legacy programs and scheduled tasks disabled-but-exported until the monitored soak, rollback drill, and operator sign-off are complete.
