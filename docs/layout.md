# RadioTEDU OnAir Layout

RadioTEDU OnAir is the current commercial product path. The repository root remains the engineering source of truth for the running application, tests, build scripts, installer, and runtime data.

## Daily Commands

- Run locally: `uvicorn app.main:app --reload`
- Verify foundation changes: `python -m pytest tests -q`
- Run the broader backend suite: `python -m pytest`
- Import legacy data: `python scripts/import_legacy_data.py`

## Backend Map

- `app/api`: HTTP surface, including legacy-compatible endpoints and the current API
- `app/engine`: worker loop, queue autofill, playout state, and recovery policy
- `app/audio`: station runtime, output health routing, audio processing, and pipeline builders
- `app/repositories`: DB access layer
- `app/static`: legacy UI assets
- `data`: database, media, tools, and runtime artifacts kept in place for now
- `tests`: unit and integration coverage

## Operational Notes

The product path is RadioTEDU OnAir. Icecast, On Air, and Playlists flow through the same backend. Build and release entrypoints are `build_backend_onefile.ps1`, `build_desktop_bundle.ps1`, and `installer/build_setup.ps1`.

The import flow keeps `music -> music` semantics, supports `hard cut` fallback behavior where needed, and uses FFmpeg/ffplay/ffprobe plus yt-dlp bootstrap on first launch into `%LOCALAPPDATA%/RadioTEDU OnAir/tools`.

The queue API now persists to SQLite so worker recovery and UI polling share the same source of truth.
