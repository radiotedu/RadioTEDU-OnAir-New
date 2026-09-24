# RadioTEDU OnAir Commissioning Report

Date: 2026-07-30  
Operator surface: `http://127.0.0.1:8110/app`  
Commissioned database: `C:\ProgramData\RadioTEDU\OnAir\cleanroom.db`

## Outcome

- The Broadcast Wall database was migrated without modifying the source file.
- Seven legacy stations, their track states, output settings, and mount mappings
  are present in the commissioned database.
- Only RadioTEDU Lo-Fi is on air. Its engine, worker loop, and Icecast encoder
  recovered automatically after a controlled backend restart.
- The legacy port-8100 Broadcast Wall, its playout guard, and its two enabled
  machine-startup tasks were stopped/disabled to prevent duplicate ownership of
  `/lofi`.
- Voting and Juke local agents are healthy but cannot publish audio: voting
  playback/HTTP/Icecast output and Juke AI mirror/autoplay are disabled.
- Ollama is healthy and externally managed. AI broadcast supervisors and public
  web backends remain disabled.

## Station and mount manifest

| Station | Configured mount | Active media |
|---|---:|---:|
| RadioTEDU Classical | `/radiotedu2` | 648 music, 21 jingles |
| RadioTEDU Lo-Fi | `/lofi` | 1,530 music, 1 jingle |
| RadioTEDU Events | `/radio` | 131 music, 1 jingle, 3 ads |
| RadioTEDU Jazz | `/radiotedu4` | 1,511 music, 21 jingles |
| RadioTEDU | `/radiotedu1` | 318 music, 7 jingles |
| Rock | `/radiotedu3` | 356 music, 7 jingles |
| RadioTEDU Energetic | `/energetic` | 196 music, 4 jingles |

The other mounts are configured but were not taken on air during commissioning,
so the operator's requirement to broadcast only `/lofi` was preserved.

## Verification evidence

- Full Python suite: 794 passed, 3 skipped, 3 subtests passed.
- Voting agent: 75 tests passed; production build passed.
- Juke media agent: 9 tests passed.
- Active-media audit: 4,759 active rows, 4,755 unique files, 4,755
  playable files, 0 failures.
- Packaged desktop smoke test: passed.
- Branded installer: `release\setup\RadioTEDU-OnAir-Setup-1.0.0.exe`
  (300.6 MB).
- Installer SHA-256 file matches the built executable.
- Installer Authenticode status: unsigned.

Machine-readable evidence:

- `C:\ProgramData\RadioTEDU\OnAir\commissioning\active-media-report-20260730.json`
- `C:\ProgramData\RadioTEDU\OnAir\commissioning\wall-migration-audit-20260730.json`
- `C:\ProgramData\RadioTEDU\OnAir\commissioning\desktop-smoke.out.log`
- `C:\ProgramData\RadioTEDU\OnAir\commissioning\installer-build.out.log`

## Mouse/UI coverage

Visible Chrome was used to exercise station selection, navigation, refresh,
start/stop/resume continuity, immediate restart policy, media search, queue
add/move/remove, managed-folder rescan, jingle upload and exact folder sync,
two- and three-song jingle rules, emergency TRT preview/takeover/stop, output
save/test, diagnostics, activity clearing, password rotation, reversible station
create/delete, service Check/Start/Stop/Restart, and repository update.

Temporary users, stations, sessions, and uploaded test files were removed.

## External follow-up

The application can authenticate as an Icecast source and metadata updates
periodically receive HTTP 200, but direct listener requests to
`10.98.98.75:11154/lofi` are currently reset by the remote host. The public
reverse-proxy URL `https://stream.radiotedu.com/lofi` returns HTTP 404. This is
outside the local OnAir process and requires an Icecast/reverse-proxy
configuration change on the web server.

Before broad public distribution, sign the installer with a trusted
Authenticode code-signing certificate; the current binary is branded and
hash-verified but unsigned.

This commissioning work improves failure containment and recovery, but it is
not a military/NATO safety certification.
