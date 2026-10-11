# Live library folders and official play reports

## Production folders

The six protected primary stations watch these roots recursively. Operators may
copy supported audio into the `Incoming` subfolder or any other subfolder beneath
the configured root.

| Station | Mount | Managed root | Drop folder |
|---|---|---|---|
| Classical | `/classic` | `H:\RadioTEDU Songs\Classical` | `...\Incoming` |
| Lo-Fi | `/lofi` | `H:\RadioTEDU Songs\lofi` | `...\Incoming` |
| Pop | `/radio` | `H:\RadioTEDU Songs\Pop` | `...\Incoming` |
| Jazz | `/cazz` | `H:\RadioTEDU Songs\Jazz` | `...\Incoming` |
| Rock | `/rock` | `H:\RadioTEDU Songs\Rock` | `...\Incoming` |
| Energize | `/energize` | `H:\RadioTEDU Songs\Energize` | `...\Incoming` |

The watcher waits for two identical file observations before importing. This
keeps partially copied files out of the library. Unreadable files are skipped
and reported without blocking good files. Transient disk, decoder, or database
errors retry forever with exponential delay capped at five minutes. Imports are
incremental and do not stop or reconnect a broadcast source.

The persisted managed-library setting is authoritative for every station. A
disabled or expired legacy campaign cannot rewrite the operator's live-folder
choice. The five-minute audio watchdog only enforces an active campaign, and
the campaign's managed roots use the same `H:\RadioTEDU Songs` folders.

## Operator process in the UI

1. Open **Media -> Managed station library** and choose the station.
2. Select the station's managed root, enable **Include subfolders** and **Skip
   unreadable files**, then choose merge or exact replacement.
3. Save/sync. The backend persists the folder, recursive choice, management
   mode, profile metadata, and skip policy; the UI reads them back.
4. After that, copy new audio into the station's `Incoming` folder. The watcher
   validates and imports it automatically. The normal deterministic queue
   refill can select it without clearing the playing item.

## Official play reports

Windows Scheduled Task `RadioTEDU Official Music Usage Export` runs as SYSTEM at
startup (two-minute delay) and every five minutes. It ignores overlapping runs,
starts missed runs when the machine returns, and retries three times after a
task failure.

Reports are stored in `H:\RadioTEDU Official Reports\Music Usage`:

- `current\RadioTEDU-music-usage-events-current.csv` contains each immutable
  completed-play event and its ledger hash.
- `current\RadioTEDU-music-play-counts-current.csv` aggregates completed play
  count, event count, total played seconds, and first/last UTC airtime for each
  station, configured mount, and track.
- `current\RadioTEDU-music-usage-integrity-current.json` contains generation
  time, record counts, SHA-256 checksums, and full per-station hash-chain status.
- `daily\YYYY-MM-DD.csv` is the prior UTC day's event archive.
- `monthly\YYYY-MM.csv` is the immutable month-close export created on the first
  UTC day of the following month.

CSV files are written to a temporary sibling and atomically replaced, so a
power loss cannot expose a half-written current report. Historical plays from a
disabled station remain in the official record with
`mount_status=historical_or_disabled`; this never creates or restores a mount.

## Maintainer verification

Run the exporter manually without touching live audio:

```powershell
& 'C:\Users\tedu\AppData\Local\Programs\Python\Python312\python.exe' -u `
  'C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio\tools\export_official_music_usage.py' `
  --db 'C:\ProgramData\RadioTEDU\OnAir\cleanroom.db' `
  --output 'H:\RadioTEDU Official Reports\Music Usage'
```

Success requires `integrity_valid=true`. A failed chain check preserves the
last good current CSV files and writes
`RadioTEDU-music-usage-integrity-FAILED.json` for investigation.
