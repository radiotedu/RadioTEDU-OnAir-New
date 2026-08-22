# RadioTEDU live-folder operations

The folders under `H:\RadioTEDU Songs` are the six stations' authoritative music database. The music manager does not need Codex or the OnAir UI for ordinary library work.

## Music manager workflow

| Station | Live music folder | Primary mount | Lossless mount |
|---|---|---|---|
| Classical | `H:\RadioTEDU Songs\Classical` | `/classic` (Opus 192 kb/s) | `/classic-flac` (Ogg FLAC) |
| Lo-Fi | `H:\RadioTEDU Songs\lofi` | `/lofi` (Opus 192 kb/s) | `/lofi-flac` (Ogg FLAC) |
| Pop | `H:\RadioTEDU Songs\Pop` | `/radio` (Opus 192 kb/s) | `/radio-flac` (Ogg FLAC) |
| Jazz | `H:\RadioTEDU Songs\Jazz` | `/cazz` (Opus 192 kb/s) | `/cazz-flac` (Ogg FLAC) |
| Rock | `H:\RadioTEDU Songs\Rock` | `/rock` (Opus 192 kb/s) | `/rock-flac` (Ogg FLAC) |
| Energize | `H:\RadioTEDU Songs\Energize` | `/energize` (Opus 192 kb/s) | `/energize-flac` (Ogg FLAC) |

- Add a supported audio file anywhere inside a station folder. After the copy becomes stable, it is validated and enters that station's rotation automatically.
- Delete a file to remove it from pending rotation automatically. A file already playing is allowed to finish, preventing an avoidable source disconnect.
- Edit tags or replace the audio file in place to update title, artist, album, genre, duration, and artwork automatically.
- A broken or half-copied file is skipped and retried; it does not replace the verified live database.
- Discovery waits for two unchanged watcher polls before importing a new or replaced file. Large copies therefore appear only after they are stable, normally about ten seconds after copying finishes.
- File selection is deterministic: least-played and least-recently-played items are chosen first.

## Jingles and ads

- Genre jingles: `H:\RadioTEDU Songs\Ads\Jingles\<Genre>`
- Global advertisements: `H:\RadioTEDU Songs\Ads\Ads`
- Cadence: three music tracks, then one genre jingle, then one global ad when an ad exists.
- Jingles and ads use least-used rotation and avoid an immediate repeat when alternatives exist.
- French filenames were not imported. Keep French jingles out of these six local-station folders.
- If a genre jingle folder becomes empty, its break automation disables itself. Adding the first valid jingle enables it again automatically.

## Metadata and artwork

Use standard file tags for title, artist, album, genre, language, MusicBrainz recording ID, and BPM. Put same-name artwork beside a song (`Song.flac` + `Song.jpg`) or use `cover.jpg`, `folder.jpg`, or embedded artwork. Changed artwork is copied/extracted into the protected OnAir media cache.

Icecast source metadata carries the current title/artist to the normal quality
branches, including FLAC where configured. Lo-Fi deliberately suppresses its
per-track `StreamTitle` on `/lofi` and `/lofi-low`, while retaining the public
station name/genre/description. Icecast's source protocol has no standard
per-track image field, so artwork is exposed through the OnAir now-playing/media
API for compatible players; it is not injected into the audio stream.

## Official reports

- Local continuously refreshed evidence: `H:\RadioTEDU Official Reports\Music Usage`
- GitHub nightly cumulative report: `reports/music-usage/cumulative.csv`
- GitHub nightly daily report: `reports/music-usage/daily/YYYY-MM-DD.csv`
- The nightly Windows task runs after midnight, commits only these CSV files, pushes the current OnAir branch, starts late after an outage, and retries failures.

## Engineering edit map

- Folder discovery, stability checks, and retries: `app/services/managed_library_watcher.py`
- Exact add/delete/tag/art synchronization: `app/api/legacy.py`, function `_sync_station_library_folder_with_connection`
- Three-song jingle/ad queue policy: `app/engine/broadcast_queue_autofill.py` and the continuously running worker path in `app/engine/station_worker.py`
- Opus/FLAC output definitions and validation: `app/services/quality_outputs.py`
- Station audio queues, silence continuity, and output recovery: `app/audio/station_runtime.py`, `app/audio/icecast_audio_sink.py`, and `app/engine/runtime_registry.py`
- One-command persisted commissioning: `tools/commission_live_folder_broadcast.py`
- Nightly GitHub CSV publishing: `tools/publish_music_usage_to_github.py`
- Continuous local official export: `tools/export_official_music_usage.py`

After an engineering change: take an OnAir recovery backup, run focused tests, hot-apply output-only changes where possible, and restart the Windows supervisor only in a planned maintenance window when source code must be reloaded. Playback recovery must retain the current queue item and elapsed offset; reconnect cooldown must never mark a song or long jingle complete. Verify all primary mounts first, then verify FLAC mounts one at a time.
