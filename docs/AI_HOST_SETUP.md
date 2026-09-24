# AI Host Setup for Radio TEDU

> Note: this setup guide reflects the earlier model-download-first version. For the current operational fallback-based queue-native AI host, use [AI_HOST_HANDOFF.md](AI_HOST_HANDOFF.md).

## Overview
The AI Host service combines:
1. **LLM (Qwen2.5-0.5B-Instruct)** - Generates intelligent commentary about music
2. **TTS (Qwen3-TTS-12Hz-1.7B-VoiceDesign)** - Speaks the commentary in a natural voice

Together, they work like a real radio host, introducing tracks with interesting context.

## Installation

### Step 1: Install AI Dependencies
```bash
pip install transformers torch qwen-tts
```

### Step 2: Download AI Models
The models need to be downloaded. Create these directories:

```powershell
# For Qwen3-TTS
mkdir C:\RadioTEDU-OnAir\Qwen3-TTS-12Hz-1.7B-VoiceDesign

# The Qwen2.5-0.5B-Instruct LLM will be downloaded from HuggingFace on first use
```

Download Qwen3-TTS model from HuggingFace:
```bash
# Using git (requires git-lfs)
git clone https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign ^
  C:\RadioTEDU-OnAir\Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

Or use the `huggingface-cli`:
```bash
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign ^
  --local-dir C:\RadioTEDU-OnAir\Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

### Step 3: Enable AI Host
Add this to your environment variables or set it in the database:

```powershell
# Enable AI host
$env:AI_HOST_ENABLED = "true"
```

Or enable it in the database:
```sql
INSERT INTO station_settings (station_id, key, value) 
VALUES (1, 'ai_host_enabled', 'true')
ON CONFLICT(station_id, key) DO UPDATE SET value='true';
```

### Step 4: Restart Server
```bash
python run_cleanroom.py
```

## How It Works

1. When a music track is about to play, the station worker checks if AI is enabled
2. The LLM generates a 2-3 sentence introduction about the composer/piece
3. The TTS engine converts that text to speech
4. The announcement plays before the music track starts
5. The announcement is cached so it's not regenerated

## Voice Personas

The AI host changes voice style based on time of day:
- **Morning (5-12)**: Bright, warm, slightly upbeat
- **Afternoon (12-17)**: Confident, informative, steady
- **Evening (17-21)**: Reflective, rich, transitioning
- **Night (21-5)**: Calm, intimate, slow

## Music History Database

The AI can reference "This Day in Music History" events. The database auto-seeds with 16 classical music events.

## Troubleshooting

### AI not speaking?
- Check logs for "AI host introduction failed"
- Verify models are downloaded
- Check `ai_host_enabled` setting in database

### Out of memory?
- The models require ~4GB RAM total
- Close other applications
- Consider using CPU-only mode (slower but less memory)

### Slow generation?
- First run downloads models (1-2 GB)
- CPU mode takes 10-30 seconds per announcement
- GPU mode (CUDA) is much faster

## Disable AI Host

To temporarily disable without deleting models:
```sql
UPDATE station_settings SET value='false' 
WHERE station_id=1 AND key='ai_host_enabled';
```

Or set environment variable:
```powershell
$env:AI_HOST_ENABLED = "false"
```
