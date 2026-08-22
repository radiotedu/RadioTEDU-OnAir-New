"""Reconcile RadioTEDU library tags with MusicBrainz and the local database.

The tool is deliberately conservative: existing non-empty tags are preserved,
MusicBrainz is only used for high-confidence filename/tag matches, and every
changed file gets a tag-only backup before it is written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.file_security import audio_upload_extensions

try:
    import musicbrainzngs
    from mutagen import File as MutagenFile
    import requests
except Exception:  # pragma: no cover - optional at runtime
    musicbrainzngs = None
    MutagenFile = None
    requests = None


DB_PATH = Path(os.environ.get("CLEANROOM_DB_PATH", r"C:\ProgramData\RadioTEDU\OnAir\cleanroom.db"))
STATE_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir"
CACHE_PATH = STATE_ROOT / "metadata" / "musicbrainz-cache.json"
AUDIO_EXTENSIONS = set(audio_upload_extensions()) | {".wma"}
MARKER_RE = re.compile(
    r"\s*(?:\[[^\]]*\]|\([^)]*(?:official|video|lyrics?|youtube|spotdown)[^)]*\))\s*$",
    re.IGNORECASE,
)
PREFIX_RE = re.compile(r"^\s*\d{1,4}\s*(?:(?:__+)|(?:[-._)])|(?:\s-\s))\s*", re.IGNORECASE)
SEPARATORS = (" - ", " – ", " — ", " _ ")
_LAST_MB_REQUEST = 0.0


def canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def norm_text(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_piece(value: str) -> str:
    value = str(value or "").replace("\u2018", "'").replace("\u2019", "'")
    value = re.sub(r"\s*\[[^\]]*\]\s*$", "", value)
    value = re.sub(r"\s*__+\s*\[[^\]]*\]\s*$", "", value)
    value = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", value)
    value = MARKER_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-–—")
    return value


def parse_filename(path: Path, root: Path) -> dict[str, str]:
    stem = clean_piece(path.stem)
    stem = PREFIX_RE.sub("", stem)
    artist = ""
    title = stem
    for separator in SEPARATORS:
        if separator in stem:
            left, right = stem.split(separator, 1)
            if left.strip() and right.strip():
                artist, title = clean_piece(left).lstrip("@"), clean_piece(right)
                break
    rel_parent = path.parent
    try:
        rel_parent = path.parent.relative_to(root)
    except ValueError:
        pass
    album = ""
    parent = str(rel_parent).replace("\\", "/").strip("./")
    if parent:
        leaf = path.parent.name.strip()
        if leaf and leaf.lower() not in {"downloads", "general", "audio"} and not re.fullmatch(r"\d{1,2}[.\-]\d{1,2}", leaf):
            album = clean_piece(leaf)
    return {"title": title, "artist": artist, "album": album}


def load_cache() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_cache(cache: dict[str, dict[str, str]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CACHE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CACHE_PATH)


def musicbrainz_lookup(artist: str, title: str, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    key = f"{norm_text(artist)}\t{norm_text(title)}"
    if key in cache:
        return cache[key]
    if not artist or not title or requests is None:
        cache[key] = {}
        return {}
    try:
        global _LAST_MB_REQUEST
        wait = 1.1 - (time.monotonic() - _LAST_MB_REQUEST)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": f'artist:"{artist}" AND recording:"{title}"', "fmt": "json", "limit": 5},
            headers={"User-Agent": "RadioTEDU-OnAir/1.0 (https://github.com/radiotedu/RadioTEDU-OnAir-New)"},
            timeout=12,
        )
        _LAST_MB_REQUEST = time.monotonic()
        response.raise_for_status()
        result = response.json()
        best: dict[str, str] = {}
        for item in result.get("recordings", []):
            score = int(item.get("score", 0) or 0)
            item_title = str(item.get("title", ""))
            credits = item.get("artist-credit", []) or []
            item_artist = " ".join(str(part.get("artist", {}).get("name", "")) for part in credits if isinstance(part, dict))
            if score < 90 or norm_text(item_title) != norm_text(title):
                continue
            if artist and norm_text(artist) not in norm_text(item_artist) and norm_text(item_artist) not in norm_text(artist):
                continue
            releases = item.get("releases", []) or []
            best = {
                "title": item_title,
                "artist": item_artist or artist,
                "album": str(releases[0].get("title", "")) if releases else "",
                "musicbrainz_recordingid": str(item.get("id", "")),
            }
            break
        cache[key] = best
        return best
    except Exception as exc:  # network/rate-limit errors are non-fatal
        cache[key] = {"_error": type(exc).__name__}
        return {}


def read_rows(conn: sqlite3.Connection) -> dict[tuple[int, str], dict]:
    rows: dict[tuple[int, str], dict] = {}
    for row in conn.execute("select * from tracks where track_type='music'"):
        item = dict(row)
        key = (int(item["station_id"]), canonical(item.get("file_path", "")))
        old = rows.get(key)
        completeness = (
            int(item.get("is_active") or 0),
            bool(str(item.get("artist") or "").strip()),
            bool(str(item.get("album") or "").strip()),
        )
        old_completeness = (
            int(old.get("is_active") or 0),
            bool(str(old.get("artist") or "").strip()),
            bool(str(old.get("album") or "").strip()),
        ) if old else (-1, False, False)
        if old is None or completeness > old_completeness:
            rows[key] = item
    return rows


def tag_snapshot(path: Path) -> dict[str, list[str]]:
    if MutagenFile is None:
        return {}
    try:
        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return {}
        return {str(k): [str(v) for v in value] for k, value in (audio.tags or {}).items()}
    except Exception:
        return {}


def write_tags(path: Path, values: dict[str, str]) -> bool:
    if MutagenFile is None or path.suffix.lower() == ".wma":
        return False
    try:
        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        for key, value in values.items():
            if value:
                audio[key] = [value]
        audio.save()
        return True
    except Exception:
        return False


def discover(conn: sqlite3.Connection) -> list[tuple[int, Path]]:
    return [
        (int(row["station_id"]), Path(str(row["value"])))
        for row in conn.execute(
            "select station_id,value from station_settings where key='music_library_folder' and trim(value)<>'' order by station_id"
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-queries", type=int, default=250)
    parser.add_argument("--backup", type=Path, default=None)
    args = parser.parse_args()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    profiles = discover(conn)
    rows = read_rows(conn)
    cache = load_cache()
    candidates: list[dict] = []
    seen_keys: set[str] = set()
    for station_id, root in profiles:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            row = rows.get((station_id, canonical(path)), {})
            parsed = parse_filename(path, root)
            title = str(row.get("title") or "").strip() or parsed["title"]
            artist = str(row.get("artist") or "").strip() or parsed["artist"]
            album = str(row.get("album") or "").strip() or parsed["album"]
            if not artist or not title or (artist and album):
                continue
            key = f"{norm_text(artist)}\t{norm_text(title)}"
            candidates.append({"station_id": station_id, "root": root, "path": path, "row": row, "title": title, "artist": artist, "album": album, "key": key})
            seen_keys.add(key)
    print(json.dumps({"profiles": len(profiles), "candidate_files": len(candidates), "unique_queries": len(seen_keys), "dry_run": args.dry_run}, ensure_ascii=True))
    if args.dry_run:
        return 0
    if musicbrainzngs is not None:
        musicbrainzngs.set_useragent("RadioTEDU-OnAir", "1.0", "https://github.com/radiotedu/RadioTEDU-OnAir-New")
        musicbrainzngs.set_rate_limit(1.1)
    backup: dict[str, dict] = {}
    query_cache: dict[str, dict[str, str]] = {}
    queried = 0
    changed = 0
    skipped = 0
    for item in candidates:
        key = item["key"]
        mb = query_cache.get(key, {})
        if key not in query_cache and queried < max(0, args.max_queries):
            mb = musicbrainz_lookup(item["artist"], item["title"], cache)
            query_cache[key] = mb
            queried += 1
            if queried % 25 == 0:
                save_cache(cache)
        values = {
            "title": mb.get("title") or item["title"],
            "artist": mb.get("artist") or item["artist"],
            "album": mb.get("album") or item["album"],
            "musicbrainz_recordingid": mb.get("musicbrainz_recordingid") or str(item["row"].get("musicbrainz_recordingid") or ""),
        }
        row = item["row"]
        if all(str(row.get(field) or "").strip() == str(values[field] or "").strip() for field in ("title", "artist", "album")) and not mb.get("musicbrainz_recordingid"):
            continue
        path = item["path"]
        if not args.dry_run:
            snap = tag_snapshot(path)
            if snap:
                backup[str(path)] = snap
            if not write_tags(path, values):
                skipped += 1
                continue
            conn.execute(
                "update tracks set title=?,artist=?,album=case when ?<>'' then ? else album end,musicbrainz_recordingid=case when ?<>'' then ? else musicbrainz_recordingid end where station_id=? and lower(file_path)=lower(?)",
                (values["title"], values["artist"], values["album"], values["album"], values["musicbrainz_recordingid"], values["musicbrainz_recordingid"], item["station_id"], str(path)),
            )
            changed += 1
            if args.backup and changed % 25 == 0:
                args.backup.parent.mkdir(parents=True, exist_ok=True)
                args.backup.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    if backup and args.backup:
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        args.backup.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.commit()
    save_cache(cache)
    print(json.dumps({"candidate_files": len(candidates), "queries": queried, "changed": changed, "skipped": skipped, "tag_backup": str(args.backup) if backup and args.backup else ""}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


