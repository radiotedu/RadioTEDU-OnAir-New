"""
Voice presets for Qwen3-TTS voice design.

Each preset provides a detailed, natural-language description that the TTS model
uses to shape timbre, emotion, prosody, and delivery style. The descriptions are
crafted to match the model's expected input format — specific, multi-dimensional,
and using natural acoustic/performance terminology.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class VoicePreset:
    """A voice preset with detailed TTS instruct prompt and metadata."""
    preset_id: str
    display_name: str
    description: str
    instruct_prompt: str
    icon: str = "🎙️"
    category: str = "general"  # general, morning, evening, classical, energetic, calm
    edge_tts_voice: str = "en-US-GuyNeural"  # Default edge-tts voice
    edge_tts_rate: str = "+0%"  # Speech rate adjustment


# ── Voice Preset Library ─────────────────────────────────────────────────────

VOICE_PRESETS: dict[str, VoicePreset] = {
    "warm_radio_host": VoicePreset(
        preset_id="warm_radio_host",
        display_name="Warm Radio Host",
        description="Friendly, conversational radio voice — like a trusted friend",
        instruct_prompt=(
            "Warm, friendly radio host voice. Mid-range pitch with natural variation. "
            "Moderate tempo with slight pauses for emphasis. Clear articulation with "
            "a relaxed, conversational tone. Sounds approachable and genuine, like "
            "sharing interesting news with a friend. Slight smile in the voice."
        ),
        icon="🎙️",
        category="general",
        edge_tts_voice="en-US-GuyNeural",
        edge_tts_rate="-5%",
    ),
    "professional_announcer": VoicePreset(
        preset_id="professional_announcer",
        display_name="Professional Announcer",
        description="Clear, authoritative delivery — BBC/classical music presenter style",
        instruct_prompt=(
            "Professional, authoritative announcer voice. Steady mid-to-low pitch with "
            "precise, crisp articulation. Measured pace with deliberate pauses between "
            "key information. Confident and cultured tone, like a BBC Radio 3 presenter. "
            "Formal but not stiff — educated warmth with excellent diction."
        ),
        icon="📻",
        category="general",
        edge_tts_voice="en-US-ChristopherNeural",
        edge_tts_rate="-10%",
    ),
    "casual_friend": VoicePreset(
        preset_id="casual_friend",
        display_name="Casual Friend",
        description="Relaxed, natural conversation tone — like chatting over coffee",
        instruct_prompt=(
            "Casual, relaxed conversational voice. Natural pitch with slight upward "
            "inflections at phrase ends. Comfortable, unhurried tempo with organic "
            "pauses as if thinking while speaking. Warm and intimate tone, like "
            "recommending something great to a close friend. Slight breathiness for "
            "naturalness."
        ),
        icon="☕",
        category="general",
        edge_tts_voice="en-US-JennyNeural",
        edge_tts_rate="+0%",
    ),
    "energetic_morning": VoicePreset(
        preset_id="energetic_morning",
        display_name="Energetic Morning Show",
        description="Upbeat, high-energy morning show voice to start the day",
        instruct_prompt=(
            "Bright, energetic morning radio voice. Slightly higher pitch with dynamic "
            "range and enthusiasm. Upbeat tempo with quick, crisp delivery. Expressive "
            "and animated tone with natural excitement. Sounds like a morning show host "
            "who genuinely loves starting the day. Varied intonation to maintain listener "
            "engagement."
        ),
        icon="☀️",
        category="morning",
        edge_tts_voice="en-US-ChristopherNeural",
        edge_tts_rate="+10%",
    ),
    "smooth_evening": VoicePreset(
        preset_id="smooth_evening",
        display_name="Smooth Evening Voice",
        description="Rich, slower-paced evening voice — intimate and reflective",
        instruct_prompt=(
            "Smooth, rich evening voice. Lower pitch range with gentle, flowing delivery. "
            "Slower, deliberate tempo with natural pauses for reflection. Intimate and "
            "warm tone, like late-night radio companion. Softer articulation with a "
            "relaxed, unhurried quality. Slightly breathy for a cozy, inviting feel."
        ),
        icon="🌙",
        category="evening",
        edge_tts_voice="en-US-EricNeural",
        edge_tts_rate="-15%",
    ),
    "classical_host": VoicePreset(
        preset_id="classical_host",
        display_name="Classical Music Host",
        description="Refined, cultured voice for classical music programming",
        instruct_prompt=(
            "Refined, cultured classical music presenter. Rich, resonant mid-to-low pitch "
            "with elegant, polished articulation. Moderate, dignified tempo with graceful "
            "pauses between musical references. Scholarly yet accessible warmth, like a "
            "knowledgeable concert hall announcer. Measured excitement that respects the "
            "music's artistry."
        ),
        icon="🎵",
        category="classical",
        edge_tts_voice="en-US-RogerNeural",
        edge_tts_rate="-5%",
    ),
    "jazz_lounge": VoicePreset(
        preset_id="jazz_lounge",
        display_name="Jazz Lounge",
        description="Smooth, cool delivery perfect for jazz and lounge programming",
        instruct_prompt=(
            "Smooth, cool jazz lounge announcer. Relaxed low-to-mid pitch with a "
            "laid-back, effortless delivery. Slightly slower tempo with rhythmic, "
            "syncopated phrasing that mirrors jazz sensibility. Sophisticated, "
            "worldly tone with a hint of playful confidence. Like a late-night "
            "jazz club host who knows all the best stories."
        ),
        icon="🎷",
        category="calm",
        edge_tts_voice="en-US-AriaNeural",
        edge_tts_rate="-10%",
    ),
    "news_bulletin": VoicePreset(
        preset_id="news_bulletin",
        display_name="News Bulletin",
        description="Crisp, professional news reader — clear and informative",
        instruct_prompt=(
            "Professional news bulletin reader. Clear, steady mid-pitch with precise "
            "articulation of every word. Brisk, efficient tempo with brief pauses "
            "between information points. Authoritative and trustworthy tone, like "
            "a seasoned broadcast journalist. Neutral emotion with slight emphasis "
            "on key facts."
        ),
        icon="📰",
        category="general",
        edge_tts_voice="en-US-DavisNeural",
        edge_tts_rate="+5%",
    ),
}

# ── Time-Based Preset Mapping ─────────────────────────────────────────────────

_TIME_PRESET_MAP: dict[str, str] = {
    "morning": "energetic_morning",
    "afternoon": "warm_radio_host",
    "evening": "smooth_evening",
    "night": "smooth_evening",
}


def get_preset_for_time_of_day() -> VoicePreset:
    """Get the recommended voice preset based on current time of day."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        time_key = "morning"
    elif 12 <= hour < 17:
        time_key = "afternoon"
    elif 17 <= hour < 21:
        time_key = "evening"
    else:
        time_key = "night"
    preset_id = _TIME_PRESET_MAP.get(time_key, "warm_radio_host")
    return VOICE_PRESETS[preset_id]


def get_preset(preset_id: str) -> VoicePreset | None:
    """Get a voice preset by ID. Returns None if not found."""
    return VOICE_PRESETS.get(preset_id)


def list_presets(category: str | None = None) -> list[dict[str, Any]]:
    """List all presets, optionally filtered by category."""
    presets = list(VOICE_PRESETS.values())
    if category:
        presets = [p for p in presets if p.category == category]
    return [
        {
            "preset_id": p.preset_id,
            "display_name": p.display_name,
            "description": p.description,
            "icon": p.icon,
            "category": p.category,
            "instruct_prompt": p.instruct_prompt,
        }
        for p in presets
    ]


# ── Backward Compatibility ────────────────────────────────────────────────────

# Map old persona names to new preset IDs for migration
_LEGACY_PERSONA_MAP: dict[str, str] = {
    "morning": "energetic_morning",
    "afternoon": "warm_radio_host",
    "evening": "smooth_evening",
    "night": "smooth_evening",
}


def legacy_persona_to_preset(legacy_persona: str) -> VoicePreset:
    """Convert a legacy persona name to a VoicePreset."""
    preset_id = _LEGACY_PERSONA_MAP.get(legacy_persona, "warm_radio_host")
    return VOICE_PRESETS[preset_id]


def get_instruct_prompt(preset_or_persona: str) -> str:
    """
    Get the instruct prompt for a preset ID or legacy persona name.
    Falls back to the warm_radio_host preset if not found.
    """
    preset = get_preset(preset_or_persona)
    if preset is not None:
        return preset.instruct_prompt
    # Try legacy mapping
    preset = legacy_persona_to_preset(preset_or_persona)
    return preset.instruct_prompt
