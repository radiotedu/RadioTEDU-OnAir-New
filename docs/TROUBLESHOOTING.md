# RadioTEDU OnAir Troubleshooting

This guide assumes the installed Windows build. Application binaries belong in
`%ProgramFiles%\RadioTEDU\OnAir`; mutable station data belongs in
`%ProgramData%\RadioTEDU\OnAir`; per-user secrets and WebView state belong in
`%LOCALAPPDATA%\RadioTEDU\OnAir`.

Do not copy files into the old RadioTEDU Broadcast Wall installation and do not
delete either product's database while diagnosing a problem.

## Dashboard does not open

1. Check the RadioTEDU OnAir tray agent and use **Open control panel**.
2. Open `http://127.0.0.1:8100/api/health` locally. A JSON response proves the
   backend is listening even if the desktop shell is not.
3. Check whether another program already owns port `8100`. Set
   `CLEANROOM_PORT` to a free loopback port only when intentionally changing
   the whole desktop deployment.
4. Inspect the OnAir logs and the dependency state file under
   `%ProgramData%\RadioTEDU\OnAir`.
5. If the backend reports healthy but WebView is blank, repair or install the
   Microsoft Edge WebView2 Runtime, then restart the OnAir agent.

Do not bind `CLEANROOM_HOST` to `0.0.0.0` until HTTPS, authentication, CORS, and
trusted-proxy behavior are configured.

## First sign-in fails

Fresh installations do not use a fixed password. Read:

`%ProgramData%\RadioTEDU\OnAir\initial-admin-password.txt`

Sign in locally as `admin`, then change the password immediately. A successful
administrator password change removes that one-time credential file.

If the file is absent and the administrator password is unknown, preserve the
database and logs before requesting a controlled credential recovery. Do not
replace the database with a blank one.

## Startup takes a long time

The first launch may validate or stage FFmpeg, FFprobe, FFplay, and yt-dlp.
Optional AI model warming can also take time. Check `/api/health` and the
dependency status instead of repeatedly starting more backend processes.

For an isolated diagnostic run, these flags suppress optional work:

- `CLEANROOM_SKIP_STARTUP_AI=1`
- `CLEANROOM_SKIP_WORKER_AUTOSTART=1`
- `CLEANROOM_DISABLE_LIBRARY_WATCHER=1`

They are smoke-test controls, not normal production settings.

## Icecast connection is rejected

Icecast output is disabled by default. Before enabling it:

1. Confirm the selected station and output profile.
2. Confirm host, port, mount (including its leading `/`), source username, and
   source password.
3. Confirm the selected codec is accepted by the destination.
4. Use **Test stream destination**. This performs a short controlled validation;
   it does not authorize a program broadcast.
5. Check the structured output error and recovery state shown in the dashboard.

Do not paste credentials into logs or issue reports. Credentials are stored
through the protected credential vault and are redacted from API responses.

## Broadcast is stopped or degraded

Use **Stop stream — keep playlist** for an intentional stop. The button requires
a second confirmation, disables restart autostart for that station, stops the
scheduler and outputs, and verifies that the queue count and order were
preserved. The interrupted item restarts from the beginning after **Start /
resume broadcast**.

- **Stopped** means the worker/runtime is not producing a program.
- **Degraded** means OnAir detected a recoverable output or media failure.
- **Failed** means bounded recovery was exhausted and operator action is
  required.

Check, in order:

1. Station selection and station-output enablement.
2. Queue contents and the current program generation.
3. Media-file existence, readability, and codec validation.
4. FFmpeg process state and the reported exit reason.
5. Icecast reachability and authentication.
6. Recovery attempt count and next retry time.

OnAir does not retry forever. It preserves an explicit failure state so an
operator can correct the cause and start again deliberately.

## Managed media does not appear

1. Confirm the managed folder in the dashboard.
2. Confirm the Windows account running OnAir can read the folder and files.
3. Use **Sync folder and verify station library**.
4. Use **Rescan managed folder now** after correcting files.
5. Review rejected/duplicate counts in the returned import summary.

Malformed, missing, outside-root, or unstable files are rejected rather than
silently added. The watcher waits for files to become stable before importing
them.

## Jingle, advertisement, or schedule did not run

- Confirm the correct station is selected.
- Confirm the rule is enabled and its time zone/time window is correct.
- Confirm the referenced playlist or media item is active and playable.
- Check cooldown, priority, every-N-song, and conflict rules.
- Check the visible program queue rather than an internal worker queue.
- For advertisements, confirm the hourly advertising policy is enabled.

The schedule and jingle engines write explicit events for applied, skipped, and
conflicting rules.

## Microphone has no audio

1. Grant microphone permission to the desktop/browser surface.
2. Select the intended input device.
3. Confirm the meter moves before taking the microphone live.
4. Confirm gain, push-to-talk/live state, and ducking settings.
5. For remote WebRTC use, configure TURN as well as STUN and use HTTPS/WSS.
6. Stop the microphone session and reconnect after a device disconnect.

Never test a microphone against a production mount without explicit
live-broadcast authorization.

## AI host is unavailable

AI is optional. Disable the AI host and continue normal music, jingle,
advertisement, schedule, microphone, and Icecast operation.

Check the AI status panel for provider, model readiness, cache state, timeout,
and last error. Do not enable `AI_PRELOAD_MODELS` on machines that cannot hold
the selected models in memory.

## Voting or Study integration is unavailable

The adapters fail closed and expose a degraded status. Core playout must
continue. Verify the configured base URL, station mapping, timeout, and
credentials without placing secrets in logs. Re-enable only after the adapter's
health check succeeds.

## Legacy import reports a warning or failure

Always start with:

```powershell
python .\scripts\import_legacy_data.py --source-db "C:\path\to\legacy.db" --target-db "C:\path\to\staging.db"
```

The default is a dry run. Review counts and warnings. Use `--apply` only after
backing up the intended target and confirming that the snapshot/staging report
is correct. A missing optional legacy table may be reported as a warning; a
schema or credential migration failure must be corrected before apply.

## Evidence to collect for support

- Product version and installer SHA-256.
- `/api/health` response with secrets removed.
- Station ID and output mode, not its password.
- Exact UTC/local timestamp and time zone.
- Structured runtime/recovery error.
- Relevant OnAir log excerpt with credentials redacted.
- Windows version, audio device, and codec profile.

Never attach the credential vault, JWT signing key, initial administrator
password, `.env`, database, or production stream password to a public issue.
