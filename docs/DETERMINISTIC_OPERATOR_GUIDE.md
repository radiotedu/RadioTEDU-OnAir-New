# Deterministic Operator Guide

This guide covers routine operation without a terminal, database editor, or Codex. Open the deterministic wall at `/app`, sign in, and choose the station at the top before every action.

## What “verified” means

The wall does not treat a button click or an HTTP response as proof. After every important mutation it reads the authoritative backend state and checks the requested invariant. If a network response drops after the backend completed an idempotent action, the wall polls and reports the observed result instead of creating a duplicate or showing a false failure.

## First station setup

1. Open **Settings** and use **Add your first station**. Enter an Icecast host, port, mount beginning with `/`, source user, source password, and codec. After registration, the same panel becomes **Add another station** and remains outside the daily On Air workspace.
2. Select the new station at the top.
3. In **Identity and broadcast output**, check the saved values, select a local monitor only if wanted, then click **Save, apply, and verify output**.
4. Click **Test stream destination**. A successful test means the destination is configured and reachable. A running station is additionally verified by healthy required runtime branches.
5. In **Managed music folder**, click **Browse folders**, select that station's music folder, choose its profile label/genre/language, leave **Exact replacement** enabled, and sync.
6. Upload jingles or choose a **Managed jingle folder**. Enable **Automatic jingles** only after at least one jingle is verified. Set **Songs between jingles** to any whole number from 1–100 (default: 2), then choose library order or random selection. The current song always finishes before the jingle.
7. Run **Installation self-check**. Each failed required item gives a direct reason. Use **Repair managed dependencies** for packaged media or AI runtimes.
8. Choose whether this station should resume automatically after an app restart, then press **Start / resume broadcast** and wait for the green verified confirmation. Fresh stations never start by themselves.

**Stop stream — keep playlist** stops the scheduler and all outputs without
clearing, advancing, or reordering the queue. If a track, jingle, scheduled
item, or advertisement was interrupted, it returns to its original position
and restarts from the beginning when the operator resumes.

## Daily playout

- Search the selected station library and click **+ Queue**. A track already pending is not duplicated.
- Use the arrow and remove controls in **Broadcast queue**. The wall verifies the new queue order after each click.
- **Remaining and forecasted songs** updates once per second. Times are recalculated from the live runtime and saved queue durations.
- Stopping is a two-click action with a 20-second confirmation window. It stops only the selected station's scheduler and runtime.

## Format isolation

Use one folder per station and enable exact replacement. The sync transaction:

1. discovers supported audio in stable path order;
2. imports new files and reactivates existing matches;
3. deactivates only the selected station's same-type files outside the folder;
4. removes stale pending queue/program/schedule references;
5. keeps any current song playing until its natural transition;
6. refills and verifies the station queue;
7. proves that active paths exactly match the selected folder.

Music and jingle profile settings are independent. Syncing a jingle folder cannot overwrite the station's music profile, and the engine never borrows a jingle from another station.

## AI host

Use **Complete AI configuration** to select the language model, voice provider (Local Qwen TTS, Edge TTS, or OmniVoice), persona, optional model path, prompt, timing, and history options. Save first, then generate a test voice. AI is content-only: it cannot start or stop a station, change the queue or jingle rule, or block an operator-authorized music-continuity start. Disabling AI immediately returns the station to music-continuity mode without stopping playout.

Open **Services** for the local Ollama runtime and the fixed RadioTEDU AI,
Voting, and Juke components. Ollama model names use the
`ollama:model-name` form in station AI settings. Runtime, model installation,
repository updates, and database maintenance have separate two-click guards.
Core music, microphone, emergency, and stream controls remain available when
all optional services are disabled or offline.

## Emergency Broadcast

1. Open **Emergency** and choose TRT Radyo 1, TRT FM, TRT Radyo Haber, or enter another approved HTTP/HTTPS public-service page.
2. Click **Open and preview**. This opens the source without changing the broadcast.
3. Start playback on the source page, click **Arm emergency takeover**, then click **Confirm emergency takeover** within 20 seconds.
4. Select the opened source tab in the browser prompt and enable **Share tab audio**.
5. Wait for the verified live dB value. Normal program audio is muted only after the capture and render path starts successfully.
6. Click **Stop emergency audio — restore playlist** to restore the exact saved normal music/mix settings.

The emergency path is not connected to `/lofi`. Video is never sent. Only PCM
audio from the explicitly shared browser tab is forwarded. Closing or losing
the shared track, changing station, signing out, or a failed start triggers
cleanup and restoration.

## Account and safety

- Change the bootstrap password immediately using the wall's Account panel. Passwords must contain at least eight characters.
- Never commit `.env`, `.jwt-secret`, databases, media, logs, models, release binaries, or source credentials. They are ignored by the repository.
- Keep the backend bound to `127.0.0.1` unless it is behind HTTPS and a correctly configured trusted reverse proxy.
- The station delete control requires a second click within 20 seconds and cannot delete the final station.

## Recovery

- If the wall says **Connection failed**, leave the current broadcast alone and click **Refresh** after the backend reconnects.
- If an action's response disappears, wait for its verification phase. Do not click repeatedly; safe actions already retry and read back state.
- Use **Run self-check** for output, dependencies, and AI details.
- If the tray application owns the backend, use its **Restart backend** command when a packaged dependency repair explicitly requests a restart.
- If Windows service supervision owns the backend, sign in as the OnAir administrator and use **Diagnostics → Reload backend safely**. OnAir stops scheduler/audio processes first, requeues interrupted items without changing order, and verifies that the supervisor created a different backend process. Only stations whose **Resume this station automatically when the app restarts** setting is enabled resume automatically.
- If safe reload says the updated supervisor is not active, restart `RadioTEDU.BroadcastSupervisor` once from an elevated PowerShell window or reboot the PC. Future code reloads then use the in-app verified handoff instead of Windows service control.
