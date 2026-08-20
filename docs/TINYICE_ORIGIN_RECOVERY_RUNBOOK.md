# TinyIce origin recovery runbook

Status: required before RadioTEDU public listeners or the 8 approved music-quality mounts can be commissioned.

## Why this is required

The OnAir computer is healthy and continuously produces all six local music timelines. English/French AI, voting, juke-local, and the automatic services are running. The remote TinyIce host accepts source connections but currently returns no HTTP/audio bytes to listeners on any of `/classic`, `/lofi`, `/cazz`, `/energize`, `/radio`, `/rock`, `/en`, or `/fr`.

The OnAir watchdog intentionally refuses to restart healthy local sources during this origin-wide outage. This prevents a reconnect storm and preserves each programme timeline.

## One authorized server action

1. Sign in to the TinyIce Windows server (`10.98.98.75`) with an account that can administer services or scheduled tasks.
2. Copy `tools\Repair-TinyIce-Origin.ps1` from the RadioTEDU OnAir repository to that server.
3. Open an elevated Windows PowerShell window on the server.
4. Run:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Repair-TinyIce-Origin.ps1
   ```

5. Require JSON with `"ok": true`. The script acts only when it finds exactly one TinyIce service or scheduled task. It never kills an unowned process and never reboots the host. Verification requires a readable HTTP response body, not only an open TCP port or response headers.

## Automatic recovery after TinyIce returns

- OnAir and AI source transports reconnect with bounded backoff; do not manually restart all RadioTEDU services.
- Wait for the next `RadioTEDU OnAir - Audio Watchdog` run or run it once from Task Scheduler.
- Confirm the eight unsuffixed legacy listeners first.
- Keep all 8 approved quality mounts enabled and retrying. Do not auto-disable them during an origin outage; use diagnostics to verify at least 16 source slots and eventual decoded delivery for all 14 locally owned mounts.
- Canary-enable Lo-Fi low alongside its suffix-free normal stream, verify continuous listener bytes and codec/bitrate, then stage the remaining outputs one station at a time.

## Rollback and safety

- Do not delete the older RadioTEDU OnAir folder or programmes; they remain offline rollback material.
- Do not change mount credentials, mount names, Nginx configuration, or legacy encodings during the TinyIce restart.
- If the repair script reports ambiguous or missing ownership, stop and inspect the server's service/task ownership. Do not substitute a host reboot or broad process kill.
