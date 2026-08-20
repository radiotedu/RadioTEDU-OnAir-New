# Release Notes

## 1.0.0 — 2026-07-30

- Rebranded the deterministic multi-station operator surface as RadioTEDU OnAir.
- Added a dedicated RadioTEDU OnAir program mark alongside the official RadioTEDU and RTAI logos.
- Verified the current RadioTEDU deployment on `/lofi` while keeping every other station stopped unless an operator explicitly starts it.
- Kept end-user controls for start/stop, output verification, managed music and jingle folders, queue operations, emergency browser audio, timeline forecasting, readiness, and password changes.
- Made operator authority explicit: fresh stations default to stopped, restart autostart is opt-in, stop preserves the playlist, AI cannot veto broadcast control, and automatic jingles default to an adjustable every-2-songs rule.
- Blocked every legacy unattended start path when restart authorization is disabled, including scheduler, tick, track-start, and supervisor recovery calls.
- Hardened Windows FFmpeg teardown so Stop reaps the encoder before returning and cannot leave a hidden Icecast source connection behind.
- Added verified arbitrary-page emergency audio takeover with a clean return to the saved program mix.
- Prevented live status from reporting healthy when any required output branch is degraded.
- Preserved in-progress operator edits during dashboard refreshes, including enabled/interval/order controls for automatic jingles.
- Rebranded Windows release artifacts and public-facing app surfaces for RadioTEDU OnAir by RadioTEDU Technologies.
- Added first-run ready-to-stream setup state, Icecast/codec/AI warmth choices, preflight checks, and verification gates.
- Hardened non-technical Icecast deployment with server URL parsing, source credential validation through a short FFmpeg test stream, and a deployment certificate gate.
- Published the installer authoring files as open source under `installer/LICENSE.md` and documented reproducible `ISCC.exe` build inputs.
- Enabled desktop shortcut creation by default in the RadioTEDU OnAir installer.
- Added WebView2 and Ollama prerequisite bootstrap plus app-managed optional Python/Qwen payload repair hooks.
- Added release artifact and smoke validation structure for GitHub Releases.
- Replaced the single scrolling wall with persistent task-based On Air, Media, Automation, Emergency, Services, Settings, and Diagnostics workspaces.
- Replaced the emergency `/lofi` shortcut with previewable official TRT Radyo 1, TRT FM, and TRT Radyo Haber presets plus an approved custom-source workflow.
- Added Ollama as a first-class optional service with local install detection, model visibility, guarded runtime control, and fixed model installation.
- Added guarded, clean-tree, fast-forward-only repository updates for RadioTEDU AI, Voting, and Juke components while retaining backup-first database maintenance.

## Release Checklist

- Attach `RadioTEDU-OnAir-Setup-<version>.exe`.
- Attach optional portable diagnostic artifact if retained.
- Include smoke validation result from `smoke_test_desktop_bundle.ps1`.
- Add screenshots for setup wizard, On Air, Settings, and tray menu.
- Confirm license text for the release.
