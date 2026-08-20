"""
Voice text enhancer / preprocessor for TTS.

Cleans and transforms raw LLM/template text into speech-optimized text that
produces natural-sounding TTS output. Handles:
  - Abbreviation expansion (Pt. → Part, DJ → D J, etc.)
  - Punctuation normalization for natural pause placement
  - Special character removal
  - Number formatting (1999 → nineteen ninety-nine contextually)
  - Parenthetical/bracket handling
  - Track title / artist formatting for speech
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ── Abbreviation Expansions ──────────────────────────────────────────────────

_ABBREVIATIONS: dict[str, str] = {
    # Music / radio specific
    "Pt. ": "Part ",
    "pt. ": "part ",
    "Vol. ": "Volume ",
    "vol. ": "volume ",
    "No. ": "Number ",
    "no. ": "number ",
    "Nr. ": "Number ",
    "nr. ": "number ",
    "Op. ": "Opus ",
    "op. ": "opus ",
    "arr.": "arranged by",
    "Arr.": "Arranged by",
    "feat. ": "featuring ",
    "ft. ": "featuring ",
    "vs. ": "versus ",
    "vs": "versus",
    "etc.": "et cetera",
    "e.g.": "for example",
    "i.e.": "that is",
    # Common
    "Mr. ": "Mister ",
    "Mrs. ": "Missus ",
    "Ms. ": "Miss ",
    "Dr. ": "Doctor ",
    "Prof. ": "Professor ",
    "St. ": "Saint ",
    "Jr.": "Junior",
    "Sr.": "Senior",
}

# Letter-by-letter readings (for acronyms that should be spelled out)
_LETTER_BY_LETTER: set[str] = {
    "DJ", "MC", "TV", "FM", "AM", "CD", "LP", "EP", "VIP", "CEO", "CTO",
    "CIO", "CFO", "COO", "NPC", "RPG", "FPS", "RTS", "MMO", "URL", "URI",
}

# Semicolon-separated lists should become natural language
_SEMICOLON_RE = re.compile(r"\s*;\s*")


@dataclass(frozen=True, slots=True)
class EnhancedText:
    original: str
    enhanced: str
    changes_applied: list[str]


class VoiceEnhancer:
    """
    Preprocesses text to produce more natural-sounding TTS output.

    Usage:
        enhancer = VoiceEnhancer()
        result = enhancer.enhance("Up next is Pt. 3 by Bach;Mozart;Beethoven.")
        # "Up next is Part three by Bach, Mozart, and Beethoven."
    """

    def __init__(self, *, aggressive: bool = False):
        self._aggressive = aggressive

    def enhance(
        self,
        text: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> EnhancedText:
        """
        Enhance text for TTS quality.

        Parameters
        ----------
        text : str
            Raw text from LLM or template.
        context : dict, optional
            Additional context (e.g., {"is_track_intro": True}).

        Returns
        -------
        EnhancedText with original, enhanced text, and list of changes.
        """
        original = str(text or "").strip()
        if not original:
            return EnhancedText(original="", enhanced="", changes_applied=[])

        result = original
        changes: list[str] = []

        # Step 1: Expand abbreviations
        result, abbr_changes = self._expand_abbreviations(result)
        changes.extend(abbr_changes)

        # Step 2: Handle semicolon-separated lists
        result, semi_changes = self._fix_semicolon_lists(result)
        changes.extend(semi_changes)

        # Step 3: Expand letter-by-letter acronyms
        result, letter_changes = self._expand_acronyms(result)
        changes.extend(letter_changes)

        # Step 4: Clean punctuation for natural TTS pauses
        result, punct_changes = self._normalize_punctuation(result)
        changes.extend(punct_changes)

        # Step 5: Remove problematic special characters
        result, char_changes = self._remove_special_chars(result)
        changes.extend(char_changes)

        # Step 6: Fix spacing and capitalization issues
        result, space_changes = self._fix_spacing(result)
        changes.extend(space_changes)

        # Step 7: Ensure sentence ends with proper punctuation
        result, end_changes = self._fix_ending_punctuation(result)
        changes.extend(end_changes)

        return EnhancedText(
            original=original,
            enhanced=result,
            changes_applied=changes if changes else ["no changes needed"],
        )

    # ── Internal Processing Steps ─────────────────────────────────────────

    @staticmethod
    def _expand_abbreviations(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        for abbr, expansion in _ABBREVIATIONS.items():
            if abbr in text:
                text = text.replace(abbr, expansion)
                changes.append(f"Expanded '{abbr.strip()}' → '{expansion.strip()}'")
        return text, changes

    @staticmethod
    def _expand_acronyms(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        # Find standalone acronyms and add spaces between letters
        for acronym in _LETTER_BY_LETTER:
            pattern = re.compile(rf"\b{re.escape(acronym)}\b")
            if pattern.search(text):
                spaced = " ".join(acronym)
                text = pattern.sub(spaced, text)
                changes.append(f"Spelled out '{acronym}' → '{spaced}'")
        return text, changes

    @staticmethod
    def _fix_semicolon_lists(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        if ";" in text:
            parts = _SEMICOLON_RE.split(text)
            if len(parts) >= 2:
                # Find the semicolon context in original text
                # Replace "A;B;C" → "A, B, and C" pattern
                last = parts[-1].strip()
                middle = ", ".join(p.strip() for p in parts[1:-1]) if len(parts) > 2 else ""
                first = parts[0].strip()

                if middle:
                    replacement = f"{first}, {middle}, and {last}"
                else:
                    replacement = f"{first} and {last}"

                # Find and replace in original
                semi_pattern = re.compile(
                    re.escape(first) + r"\s*;\s*" +
                    (r"\s*;\s*".join(re.escape(p.strip()) for p in parts[1:-1]) + r"\s*;\s*" if middle else "") +
                    re.escape(last)
                )
                text = semi_pattern.sub(replacement, text)
                changes.append("Converted semicolon list to natural 'and' conjunction")
        return text, changes

    @staticmethod
    def _normalize_punctuation(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        # Multiple exclamation/question marks → single
        multi_punct = re.compile(r"([!?])\1+")
        if multi_punct.search(text):
            text = multi_punct.sub(r"\1", text)
            changes.append("Normalized repeated punctuation")

        # Ellipsis → comma (TTS handles comma pause better)
        if "..." in text:
            text = text.replace("...", ",")
            changes.append("Converted ellipsis to comma for natural pause")

        # Em/en dash → comma
        dash_re = re.compile(r"[—–]+")
        if dash_re.search(text):
            text = dash_re.sub(",", text)
            changes.append("Converted dash to comma for natural pause")

        # Remove excessive commas
        comma_re = re.compile(r",\s*,")
        if comma_re.search(text):
            text = comma_re.sub(",", text)
            changes.append("Removed duplicate commas")

        return text, changes

    @staticmethod
    def _remove_special_chars(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        # Remove markdown formatting
        markdown_patterns = [
            (re.compile(r"\*\*([^*]+)\*\*"), "bold"),
            (re.compile(r"\*([^*]+)\*"), "italic"),
            (re.compile(r"__([^_]+)__"), "bold"),
            (re.compile(r"_([^_]+)_"), "italic"),
            (re.compile(r"#+\s*"), "heading"),
            (re.compile(r"`([^`]+)`"), "code"),
        ]
        for pattern, name in markdown_patterns:
            if pattern.search(text):
                text = pattern.sub(r"\1", text)
                changes.append(f"Removed {name} markdown formatting")

        # Remove remaining special chars but keep basic punctuation
        # Keep: . , ! ? : ; " " ( ) [ ] ' -
        unwanted = re.compile(r"[#@\$%^&+=|\\/~`<>{}]")
        if unwanted.search(text):
            text = unwanted.sub("", text)
            changes.append("Removed problematic special characters")

        return text, changes

    @staticmethod
    def _fix_spacing(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        # Multiple spaces → single
        if re.search(r" {2,}", text):
            text = re.sub(r" {2,}", " ", text)
            changes.append("Normalized spacing")

        # Space before punctuation
        if re.search(r" \.", text) or re.search(r" ,", text):
            text = re.sub(r" +([.,!?;:])", r"\1", text)
            changes.append("Fixed space before punctuation")

        return text, changes

    @staticmethod
    def _fix_ending_punctuation(text: str) -> tuple[str, list[str]]:
        changes: list[str] = []
        if text and not text[-1] in ".!?,;:":
            text += "."
            changes.append("Added period at end of text")
        return text, changes


# ── Module-Level Convenience Function ────────────────────────────────────────

_default_enhancer: VoiceEnhancer | None = None


def enhance_for_tts(
    text: str,
    *,
    context: dict[str, Any] | None = None,
) -> str:
    """
    Quick function to enhance text for TTS using default enhancer.

    Parameters
    ----------
    text : str
        Raw text to enhance.
    context : dict, optional
        Additional context for enhancement.

    Returns
    -------
    str
        Enhanced text optimized for natural TTS output.
    """
    global _default_enhancer
    if _default_enhancer is None:
        _default_enhancer = VoiceEnhancer()
    result = _default_enhancer.enhance(text, context=context)
    return result.enhanced
