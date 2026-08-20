# RadioTEDU music-use records and reporting

RadioTEDU OnAir keeps an append-only `music_usage_log` record for every completed
music or jingle play. The row is a snapshot, so editing a library item later
cannot rewrite the historical report. Each row is hash-chained per station and
has a stable queue log ID; duplicate acknowledgements are idempotent.

The report contains:

- UTC broadcast date/time, station, work title, version, performer, composer,
  lyricist, phonogram producer, label and ISRC;
- scheduled and actual played duration, publication count (one per air event);
- source file, promo/purchase/CD reference, invoice/licence reference;
- program and presenter when a live show session is active; and the queue/log ID.

Operators maintain the repertoire fields through `PUT /api/music-usage/track-metadata/{track_id}`.
The daily export endpoint is `GET /api/music-usage/export`; the scheduled CLI is:

```powershell
py -3 tools/export_music_usage.py --date 2026-08-09
```

Exports are atomically written under the commissioned data root at
`Exports/MusicUsage/YYYY-MM-DD.csv`. On service startup, the previous UTC day's
CSV is retried automatically. The backup tool includes this directory in its
verified snapshot. At the end of the reporting period, close the month:

```powershell
py -3 tools/export_music_usage.py --month 2026-08 --closed-by operator-id
```

The `music_usage_month_closures` row records the period bounds, entry count,
first/last hash, CSV checksum and closing operator. A closed period is never
overwritten; a correction is a new append-only event. The API exposes the same
operation at `POST /api/music-usage/monthly-close`.

MESAM/MSG public examples describe reporting all published works and periodic
submissions, but RadioTEDU must use the period in its signed contract. The
monthly close is therefore an operational control, not a claim about the legal
submission deadline. Retain the closed CSV and verified backup for the contract
and applicable statutory retention period.
