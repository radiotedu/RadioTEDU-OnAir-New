# RadioTEDU Play History and GitHub backup

RadioTEDU OnAir records every completed music or jingle play in the append-only
`music_usage_log` table inside `C:\ProgramData\RadioTEDU\OnAir\cleanroom.db`.
Each row keeps the UTC timestamp, station, mount, title, artist, album/version,
source path, delivered variants, play duration, and a per-station hash-chain
entry. The SQLite ledger is the authoritative record.

## Local daily history

The background exporter and the Windows export task refresh:

`C:\Users\<operator>\Desktop\RadioTEDU Play History`

The `daily` folder contains one all-radio event CSV and one play-count CSV for
each UTC date. Every row includes `station_id`, `station_name`, and
`stream_mounts`, so the six active stations are represented in the same daily
file without losing station identity:

- Classical — station 1, `/classic`
- Lo-Fi — station 2, `/lofi`
- Pop — station 4, `/radio`
- Jazz — station 5, `/cazz`
- Rock — station 8, `/rock`
- Energize — station 9, `/energize`

The root also contains current-day aliases, all-time event/count CSVs, a JSON
manifest, and a preserved `legacy` folder. Writes are atomic; a failed export
cannot leave a half-written CSV.

## Windows tasks

These tasks are installed on the broadcast PC:

| Task | Schedule | Action |
|---|---|---|
| `RadioTEDU-OnAir-PlayHistory-Export` | Every 5 minutes | Runs `scripts\\run_play_history_export.cmd` |
| `RadioTEDU-OnAir-PlayHistory-GitHub` | Daily at 00:15 Europe/Istanbul | Runs `scripts\\run_play_history_backup.cmd` |

The GitHub task refreshes the Desktop files immediately before copying them to
the local mirror and pushing the dedicated private repository:

`https://github.com/radiotedu/RadioTEDU-OnAir-Play-History`

The backup script never deletes files from the mirror. A temporary Desktop or
network failure is therefore retried on the next run. GitHub credentials are
provided by the machine's `gh` login; no token or password is stored in this
repository.

## Manual validation

Run the export without touching the live audio workers:

```powershell
cmd /c .\\scripts\\run_play_history_export.cmd
```

Run the full export-and-push operation:

```powershell
cmd /c .\\scripts\\run_play_history_backup.cmd
```

Verify a scheduled task after a run:

```powershell
Get-ScheduledTaskInfo -TaskName 'RadioTEDU-OnAir-PlayHistory-GitHub'
```

The expected successful `LastTaskResult` is `0`. A non-zero result leaves the
local Desktop history intact and should be retried after checking `gh auth
status` and repository access.
