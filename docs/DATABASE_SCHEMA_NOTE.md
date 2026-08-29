# Database Schema (sanitized)

This snapshot documents the RadioTEDU OnAir SQLite schema without live data, credentials, or DJ activity.

- Source of truth: \C:\ProgramData\RadioTEDU\OnAir\cleanroom.db\ (production, NOT committed)
- Play ledger: \music_usage_log\ — append-only, hash-chained per station, mirrors to \play-history-exports/\
- Song reporting: \pp/services/music_usage.py\ (MusicUsageService) + \	ools/export_official_music_usage.py\
- Daily exports: \play-history-exports/daily/*.csv\
- MESAM forms: \play-history-exports/licensor/MESAM/*-radio-form.csv\

Live databases, JWT secrets, Icecast passwords, and admin hashes are excluded per \SECURITY.md\ and \README.md:204\.
To regenerate exports locally: \python tools/export_official_music_usage.py --db <path-to-db> --output ./official-reports\

