# AI Host Handoff

## App Summary

This repository is a Windows-first radio automation and live broadcast control surface.

Core subsystems:

- `app/engine/station_worker.py`: automation worker that decides what plays next
- `app/audio/station_runtime.py`: playback/runtime orchestration for local output and Icecast
- `app/api/legacy.py`: queue and playback APIs used by the current frontend
- `app/static/index.html` + `app/static/js/app.js`: main operator UI
- `app/static/js/ai-host.js` + `app/api/ai_host.py`: AI host settings and status UI/API

The operator-facing queue in the UI is built from `queue_items` plus track metadata from `tracks`.

## What Was Broken Before

Before this implementation:

- AI settings could be saved, but most of them were not actually used by the worker.
- AI intros were not queue-native.
- The worker generated an intro and directly interrupted the runtime before the music track.
- AI intros did not appear in the broadcast queue UI.
- The AI pipeline depended on local Qwen models being downloaded.
- On this machine there was no `models/` directory, so the AI host was effectively non-operational.
- There was no prompt template field, even though the user explicitly wanted prompt control.
- Status reporting was misleading because it mostly reflected model load state instead of operational readiness.

## What Changed

This turn converted the AI host into an operational, queue-aware subsystem.

### 1. Provider Fallbacks

File: `app/services/ai_host.py`

The AI host now works in this order:

- Text:
  - local Qwen LLM if present
  - template fallback if no local LLM is available
- Speech:
  - local Qwen TTS if present
  - Windows SAPI if no local TTS model is available

This matters because Windows SAPI is available on the current machine, so AI speech now has a real fallback path without any model download.

### 2. Queue-Native Announcements

Files:

- `app/engine/station_worker.py`
- `app/repositories/queue_repo.py`
- `app/repositories/track_repo.py`

The worker now inserts AI output as actual `queue_items` before the target music item.

Behavior:

- If AI host is enabled, the worker looks at the immediate next pending music track.
- If a periodic station ID is due, it inserts an `announcement` queue item first.
- If the target music item does not yet have an intro, it inserts an `announcement` queue item before it.
- Those announcement assets are stored as inactive tracks with `track_type='announcement'` so they do not pollute the music library.

Important implementation detail:

- `dedupe_key` is used to prevent the same intro from being reinserted for the same target queue item.
- The worker checks `pending`, `playing`, and `done` statuses for those dedupe keys so a played intro is not inserted again.

### 3. Runtime Awareness

File: `app/audio/station_runtime.py`

`announcement` is now a recognized runtime track type.

This prevents announcements from being treated like music for crossfade purposes.

### 4. Frontend and API

Files:

- `app/api/ai_host.py`
- `app/static/js/ai-host.js`
- `app/static/index.html`
- `app/static/js/app.js`
- `app/static/css/main.css`
- `app/static/sw.js`

Changes:

- Added `prompt_template` to the AI settings API and UI.
- AI status now reports provider information, not just model presence.
- Announcements API now returns station-scoped cached announcement metadata.
- Queue UI now renders `announcement` rows with a dedicated badge and row style.
- App shell asset versions and service worker cache version were bumped so the frontend can actually pick up the new code.

## Current AI Pipeline

### Track Intro Flow

1. `StationWorker.process_once()` autofills queue and handles jingles/startup items.
2. `StationWorker._maybe_prepare_ai_queue()` checks the immediate next pending music item.
3. If AI is enabled:
   - it may insert a periodic station ID announcement
   - it may insert a track intro announcement
4. Inserted announcement becomes a normal queue item with `track_type='announcement'`.
5. The worker plays that announcement first.
6. On the next tick, once the announcement finishes, the worker starts the actual music track.

### Text Generation

File: `app/services/ai_host.py`

Track intro text uses:

- station name
- track title
- artist/composer
- optional music history context
- optional educational clause
- prompt template from station settings

If a local LLM is unavailable, the final spoken text is rendered directly from the template.

### Speech Generation

File: `app/services/ai_host.py`

Speech output uses:

- local Qwen TTS if present
- otherwise Windows SAPI

Generated assets are cached under `data/ai_cache/`:

- `announcement_<hash>.wav`
- `announcement_<hash>.json`

`clear_cache()` currently clears in-memory cache plus JSON metadata, but intentionally leaves the WAV files in place so queued announcement tracks do not suddenly point at missing audio.

## Important Settings

Stored in `station_settings`:

- `ai_host_enabled`
- `ai_llm_model`
- `ai_tts_model_path`
- `ai_voice_persona`
- `ai_announcement_max_seconds`
- `ai_include_music_history`
- `ai_educational_segments`
- `ai_station_id_interval`
- `ai_prompt_template`
- `_ai_last_station_id_at`

Notes:

- `ai_educational_segments` currently enriches the intro text with a listening note. It does not yet schedule separate hourly long-form educational breaks.
- `ai_station_id_interval` is implemented as a periodic queue-native station ID insert before the next song when due.

## Files Most Relevant To Future Work

- `app/services/ai_host.py`
- `app/engine/station_worker.py`
- `app/repositories/queue_repo.py`
- `app/repositories/track_repo.py`
- `app/api/ai_host.py`
- `app/static/js/ai-host.js`
- `app/static/js/app.js`
- `app/static/index.html`

## Tests Added Or Updated

- `tests/unit/test_ai_host_api.py`
- `tests/unit/test_ai_host_service.py`
- `tests/unit/test_station_worker_fallback.py`

Also re-ran:

- `tests/unit/test_station_runtime.py`

## Manual Verification Path

1. Open the AI Host panel.
2. Enable AI Host.
3. Set `Station ID Interval` to `0` if you want to test only track intros.
4. Save settings.
5. Make sure there is at least one music track in the automation queue.
6. Let the worker tick.
7. The queue should show an `announcement` item before the target song.
8. The announcement should play first, then the song should start.

## Known Limits / Open Questions

- AI augmentation is currently implemented for the main automation queue, not the host program queue path.
- The fallback text mode is deterministic and operational, but it is not equivalent to a fully expressive cloud or local frontier model.
- The user previously asked about AAC+ 192k and smoother live streaming. That is not addressed in this change set.
- The user also mentioned OmniVoice specifically. The current operational fallback is Windows SAPI because it exists on this machine right now.
- There is still some mojibake in historical comments/strings in older files unrelated to this AI rewrite.

## Recommended Next Steps If Another Session Continues

- Add AI augmentation to the host/program queue if that is desired operationally.
- Decide whether educational segments should become separate scheduled queue items instead of a short clause inside the intro.
- Add a frontend preview button for generating and auditioning an AI announcement before saving settings.
- If the user still wants AAC+/stream encoder changes, continue in the runtime/Icecast sink layer rather than in the AI host layer.
