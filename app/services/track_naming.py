from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_RIGHTS_WORDS = re.compile(
    r"\b(?:no\s+copyright(?:ed)?|non[- ]?copyright(?:ed)?|copyright[- ]?free|"
    r"free\s+(?:music|download)|royalty[- ]?free|creative\s+commons)\b",
    re.IGNORECASE,
)
_RIGHTS_GROUP = re.compile(r"\s*[\[(][^\])]*(?:copyright|royalty|free\s+music)[^\])]*[\])]\s*", re.IGNORECASE)
_LEADING_GROUP = re.compile(r"^\s*\[[^\]]{1,80}\]\s*")
_TRAILING_GROUP = re.compile(r"\s*\[([^\]]{1,80})\]\s*$")
_VERSION_WORDS = re.compile(
    r"\b(?:radio\s+edit|album\s+version|remix|mix|version|instrumental|acoustic|live|extended|orchestral)\b",
    re.IGNORECASE,
)
_SEO_TAIL = re.compile(
    r"\s*(?:[/|]\s*)?(?:official\s+)?(?:music\s+)?(?:video|audio|gaming\s+music|background\s+music)\s*$",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedTrackName:
    title: str
    artist: str
    version: str = ""

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


def _without_symbols(value: str) -> str:
    kept: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category in {"So", "Cs"} or char in {"\ufe0e", "\ufe0f", "\u200d"}:
            continue
        kept.append(char)
    return "".join(kept)


def _text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("⧸", "/").replace("｜", "|")
    text = _without_symbols(text)
    return _SPACE.sub(" ", text).strip(" \t\r\n-|/")


def _remove_rights_language(value: str) -> str:
    value = _RIGHTS_GROUP.sub(" ", value)
    value = _RIGHTS_WORDS.sub(" ", value)
    return _SPACE.sub(" ", value).strip(" -|/")


def _clean_artist(value: str, fallback: str = "") -> str:
    artist = _text(value)
    was_handle = artist.lstrip().startswith("@")
    artist = artist.split("|")[0].split("/")[0]
    artist = re.sub(r"\s*\[[^\]]{1,80}\]\s*$", "", artist)
    artist = re.sub(r"\s*[-—–]\s*(?:No Copyright Music|Royalty Free Music)\s*$", "", artist, flags=re.I)
    artist = _remove_rights_language(artist)
    artist = re.sub(r"\s*[-—–]\s*Music\s*$", "", artist, flags=re.I)
    artist = re.sub(r"^@+", "", artist).strip(" -—–,;@")
    if was_handle:
        artist = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", artist)
    if not artist and fallback:
        return _clean_artist(fallback)
    return _SPACE.sub(" ", artist)


def _clean_title_and_version(value: str) -> tuple[str, str]:
    title = _text(value)
    title = _remove_rights_language(title)
    while True:
        match = _LEADING_GROUP.match(title)
        if match is None:
            break
        title = title[match.end() :].strip()
    version = ""
    trailing = _TRAILING_GROUP.search(title)
    if trailing is not None:
        candidate = _text(trailing.group(1))
        if _VERSION_WORDS.search(candidate):
            version = candidate
        title = title[: trailing.start()].strip()
    title = _SEO_TAIL.sub("", title).strip()
    if "/" in title:
        first, *tails = [part.strip() for part in title.split("/")]
        if tails and all(
            re.search(r"\b(?:video|audio|music|copyright|royalty|gaming)\b", tail, re.I)
            for tail in tails
        ):
            title = first
    title = title.strip(" \t\r\n-'\"“”‘’|/")
    title = _SPACE.sub(" ", title)
    return title or "Track", version


def normalize_track_name(
    raw_title: object,
    raw_artist: object = "",
    *,
    fallback_title: object = "Track",
) -> NormalizedTrackName:
    """Turn source/SEO-heavy media names into a stable ``Artist - Title`` identity.

    The caller remains responsible for retaining the original source title in
    compliance metadata. This function is deterministic and contains no
    network or filesystem access, so imports, API repair jobs, and tests all
    use exactly the same naming policy.
    """

    source = _text(raw_title) or _text(fallback_title) or "Track"
    source_without_rights = _remove_rights_language(source)
    fallback_artist = _clean_artist(raw_artist)
    artist = ""
    title = ""

    quoted = re.search(
        r"[\"'“”‘’](?P<title>.+)[\"'“”‘’]\s+by\s+(?P<artist>.+)$",
        source_without_rights,
        flags=re.IGNORECASE,
    )
    if quoted is not None:
        title = quoted.group("title")
        artist = quoted.group("artist")

    if not title:
        by_dash_video = re.search(
            r"\bby\s+(?P<artist>[^/|]{1,160}?)\s*-\s*(?P<title>[^/|]{1,255}?)\s*/\s*video\b",
            source_without_rights,
            flags=re.IGNORECASE,
        )
        if by_dash_video is not None:
            artist = by_dash_video.group("artist")
            title = by_dash_video.group("title")

    if not title:
        by_slash = re.search(
            r"\bby\s+(?P<artist>[^/|]{1,160}?)\s*/\s*(?P<title>[^/|]{1,255})(?:\s*/.*)?$",
            source_without_rights,
            flags=re.IGNORECASE,
        )
        if by_slash is not None:
            artist = by_slash.group("artist")
            title = by_slash.group("title")

    candidate = source_without_rights
    while True:
        leading = _LEADING_GROUP.match(candidate)
        if leading is None:
            break
        candidate = candidate[leading.end() :].strip()

    if not title and "|" in candidate:
        pipe_candidate = candidate.rsplit("|", 1)[-1].strip()
        split = re.match(r"(?P<artist>.+?)\s+-\s+(?P<title>.+)$", pipe_candidate)
        if split is not None:
            artist = split.group("artist")
            title = split.group("title")

    if not title:
        by_artist = re.match(r"(?P<title>.+?)\s+by\s+(?P<artist>[^/|]+)$", candidate, flags=re.I)
        if by_artist is not None:
            title = by_artist.group("title")
            artist = by_artist.group("artist")

    if not title:
        split = re.match(r"(?P<artist>[^/|]{1,160}?)\s*[-—–]\s+(?P<title>.+)$", candidate)
        if split is not None:
            artist = split.group("artist")
            title = split.group("title")

    if not title and "/" in candidate:
        title = candidate.rsplit("/", 1)[-1]
        artist = fallback_artist

    if not title:
        title = candidate
        artist = fallback_artist

    clean_title, version = _clean_title_and_version(title)
    clean_artist = _clean_artist(artist, fallback=fallback_artist)
    return NormalizedTrackName(title=clean_title, artist=clean_artist, version=version)
