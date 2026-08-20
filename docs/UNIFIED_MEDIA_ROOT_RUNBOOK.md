# Unified media-root runbook

The broadcast computer has one media root: `E:\RadioTEDU Media`. Treat this as
the source of truth; do not create a second copied library under an application
directory.

| Folder | Purpose |
| --- | --- |
| `Sources` | Read-only external/source-system intake references and provenance notes. |
| `Broadcast` | Curated broadcast-ready material shared by station workflows. |
| `Juke\Non-Turkish` | Juke Local’s configured music root. |
| `Voting` | Voting Radio’s configured music root. |
| `Jingles` | Approved station imaging and jingles. |
| `Ads` | Approved advertisements only. |
| `Emergency` | Short, tested emergency/fallback material. |
| `Imports` | Quarantine/staging for new files before validation and mapping. |
| `Databases` | Media indexes and database exports; never hand-edit live databases. |
| `Manifests` | Source maps, checksums, and validation manifests. |
| `Backups` | Recoverable exports and snapshots; never use as a live playout root. |

Use [unified-media-source-map.json](C:\Users\tedu\Documents\RadioTEDU-OnAir\installer\templates\unified-media-source-map.json)
as the starting source map. Copy it to `E:\RadioTEDU Media\Manifests`, update
only paths, ownership, provenance, and checksums, then validate it before any UI
refresh. Never put passwords, tokens, endpoint credentials, or media contents
in the map.

Adding media: put it in `Imports`, validate codec/rights/metadata, add its
source-map entry and checksum, then move it into its intended managed folder.
Removing media: remove it from the source map first, refresh the UI, confirm it
is no longer queued, then move it to `Backups` or approved archival storage.
Updating media: import a new version, give it a new checksum/map entry, refresh
and verify it, then retire the previous version. Do not overwrite a file that
may be in a playout queue.

Direct operator drop-ins are also supported in `Broadcast`, `Juke\Non-Turkish`,
`Voting`, `Jingles`, `Ads`, and `Emergency`. On refresh, only destinations that
the prior manifest marked as source-map-generated are replaceable. Other regular
files are verified and carried forward as hardlinks; a collision, symlink,
reparse point, path escape, or case-fold duplicate blocks the refresh without
publishing a partial view. Deleting an operator-owned file from its view leaves
it deleted—it is never reconstructed unless it appears in the source map.

After any map change, use the operator wall’s **Refresh media library** action
and wait for its completed/success state before scheduling or broadcasting the
new material. If it fails, keep playout unchanged and correct the map or import
instead of repeatedly refreshing.

Hardlinks may be used only when the source and destination are on the same NTFS
volume, the file is immutable, and the map records the canonical path. A hardlink
is not an independent backup: deleting or editing either name affects the same
data. Do not duplicate the same audio into Juke and Voting merely to satisfy two
views—use the source map/canonical ownership and a hardlink only when the above
conditions are met.

Recovery: stop scheduling new items, preserve `Manifests` and `Databases`, use
the most recent checked backup in `Backups`, restore into `Imports`, validate its
checksum and map, then refresh the library. Do not point a running service at a
backup directory.

Stage the handoff only from an elevated PowerShell session, first with `-WhatIf`:

```powershell
& 'C:\Program Files\RadioTEDU\OnAir\installer\StageBroadcastPcHandoff.ps1' -HandoffRoot 'C:\RadioTEDU-Handoff' -JukeMusicRoot 'E:\RadioTEDU Media\Juke\Non-Turkish' -VotingMusicRoot 'E:\RadioTEDU Media\Voting' -WhatIf
```

Do not remove `-WhatIf` until the source map, paths, lockfiles, and commissioning
evidence have been reviewed. The staging helper never starts services.
