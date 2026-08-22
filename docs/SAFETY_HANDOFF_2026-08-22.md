# RadioTEDU OnAir safety handoff — 2026-08-22

## Operator outcome

- The sole operator-facing application is `C:\Users\tedu\Desktop\RadioTEDU OnAir (New)`.
- Nine confirmed legacy broadcasting applications or service-source trees were moved to the reversible quarantine at `C:\Users\tedu\Desktop\Broadcasting Apps Quarantine\20260822-222907`.
- The production supervisor and backend were not restarted during quarantine. Their process start times predate the operation, and `http://127.0.0.1:18110/api/health/live` remained HTTP 200 with state `operational`.
- The desktop shortcut continues to open the UI. Folder selection is available through native Windows folder-picker controls in the UI.

## Çalışıyorsa bozma boundary

The currently running continuity backend remains at `C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio`. The automatic `RadioTEDU.OnAir.Supervisor` service, audio watchdog, and play-history/reporting tasks still point there. This is deliberate: moving or restarting that runtime during a live broadcast would violate the no-disconnection requirement. It should be migrated only in a planned, monitored handover window.

The following competing services remain stopped and disabled:

- `RadioTEDU.AIStreams`
- `RadioTEDU.JukeLocalMediaAgent`
- `RadioTEDU.SharedAI`
- `RadioTEDUVotingRadio`

## Recovery material

Primary safety root:

`C:\Users\tedu\Desktop\RadioTEDU Safety Backups\20260822-204547`

It contains a verified full-history Git bundle, a consistent SQLite online snapshot, an installed-app copy, service definitions, and a post-change source recovery set. The quarantine has its own `README-DO-NOT-RUN.txt` and `quarantine-manifest.json` with one-to-one restore paths.

## Verification record

- Service-control process tracking: 18/18 tests passed.
- Local/offline AI cache, host API, host service, fast local service, and runtime-path suites passed.
- Recovery, reliability, deterministic scheduling/operator, unified-media, and folder-sync suites passed.
- High-assurance feature suite: 26/26 tests passed.
- Browser-independent UI JavaScript suite: 74/74 tests passed.
- Voting/campaign/integration offline suite: 10/10 tests passed.
- rtAI edition/API/UI regression set passed; Python byte-compilation and JavaScript syntax checks passed.
- The rtAI archive was rebuilt twice with an identical SHA-256, demonstrating deterministic packaging.

## rtAI OnAir edition

Build directory:

`C:\Users\tedu\Desktop\RadioTEDU OnAir (New)\dist\editions\rtAI-onair-1.0.2`

ZIP:

`C:\Users\tedu\Desktop\RadioTEDU OnAir (New)\dist\editions\rtAI-onair-1.0.2.zip`

The final SHA-256 is recorded outside the package in the post-change recovery
set's `README-RECOVERY.txt`, avoiding a self-referential checksum inside the ZIP.

This edition retains local AI and core on-air operation while disabling RadioTEDU-specific voting/campaign, integration, quality-plan, and product-catalog surfaces.
