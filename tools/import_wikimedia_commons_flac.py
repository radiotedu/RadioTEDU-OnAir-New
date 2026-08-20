"""Import curated, license-checked FLAC audio from Wikimedia Commons.

The tool resolves each station's current library folder from cleanroom.db,
verifies the remote MIME type, reusable license metadata, size and SHA-1, then
downloads atomically.  Attribution stays beside the audio in a JSON manifest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "RadioTEDU-OnAir/1.0 (broadcast-library; https://radiotedu.com)"
MANIFEST_NAME = "WIKIMEDIA_COMMONS_ATTRIBUTION.json"
CATALOG_NAME = "CANDIDATE_CATALOG.csv"
README_NAME = "README - REVIEW BEFORE IMPORTING.txt"
GENRE_FOLDERS = {
    "classic": "Classical",
    "lofi": "Lofi",
    "radio": "Pop",
    "cazz": "Jazz",
    "rock": "Rock",
    "energize": "Energize",
}
ALLOWED_LICENSE_TOKENS = (
    "cc0",
    "cc by",
    "cc-by",
    "cc by-sa",
    "cc-by-sa",
    "public domain",
    "pdm",
)
PROHIBITED_LICENSE_TOKENS = (
    "noncommercial",
    "non-commercial",
    "by-nc",
    "by-nd",
    "no derivatives",
    "no-derivatives",
)


def _plain(value: object) -> str:
    text = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _request_json(params: dict[str, str]) -> dict:
    url = f"{API_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=45) as response:
        return json.load(response)


def _commons_info(title: str) -> dict:
    payload = _request_json(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "imageinfo",
            "titles": title,
            "iiprop": "url|mime|size|sha1|extmetadata",
            "iiurlwidth": "1200",
        }
    )
    page = (payload.get("query", {}).get("pages") or [{}])[0]
    if page.get("missing") is True or not page.get("imageinfo"):
        raise RuntimeError(f"Commons file does not exist: {title}")
    info = page["imageinfo"][0]
    metadata = {
        key: item.get("value", "")
        for key, item in (info.get("extmetadata") or {}).items()
        if isinstance(item, dict)
    }
    license_name = _plain(metadata.get("LicenseShortName"))
    license_url = _plain(metadata.get("LicenseUrl"))
    license_probe = f"{license_name} {license_url}".lower()
    if any(token in license_probe for token in PROHIBITED_LICENSE_TOKENS):
        raise RuntimeError(
            f"Commons license is not suitable for unrestricted RadioTEDU reuse for {title}: "
            f"{license_name or 'unknown'}"
        )
    if not any(token in license_probe for token in ALLOWED_LICENSE_TOKENS):
        raise RuntimeError(
            f"Commons license is absent or not allowlisted for {title}: "
            f"{license_name or 'unknown'}"
        )
    mime = str(info.get("mime") or "").lower()
    canonical_title = str(page.get("title") or title)
    if mime not in {"audio/flac", "audio/x-flac"} or not canonical_title.lower().endswith(".flac"):
        raise RuntimeError(f"Commons item is not original FLAC audio: {canonical_title} ({mime})")
    return {
        "title": canonical_title,
        "download_url": info["url"],
        "description_url": info.get("descriptionurl") or "",
        "mime": mime,
        "bytes": int(info.get("size") or 0),
        "sha1": str(info.get("sha1") or "").lower(),
        "artist": _plain(metadata.get("Artist")) or _plain(metadata.get("Credit")),
        "credit": _plain(metadata.get("Credit")),
        "license": license_name,
        "license_url": license_url,
        "attribution_required": _plain(metadata.get("AttributionRequired")),
        "source": _plain(metadata.get("Source")),
        "preview_url": str(info.get("thumburl") or ""),
        "preview_mime": str(info.get("thumbmime") or "").lower(),
    }


def _safe_filename(title: str) -> str:
    name = title.removeprefix("File:").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
    if not name.lower().endswith(".flac"):
        name += ".flac"
    return f"Wikimedia Commons - {name}"


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(info: dict, destination: Path, backup_root: Path, dry_run: bool) -> str:
    expected_sha1 = info["sha1"]
    expected_bytes = info["bytes"]
    if destination.exists():
        if destination.stat().st_size == expected_bytes and _sha1(destination) == expected_sha1:
            return "unchanged"
        if dry_run:
            return "would-replace"
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(backup_root / destination.name))
    if dry_run:
        return "would-download"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".part", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        request = Request(info["download_url"], headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if temporary.stat().st_size != expected_bytes:
            raise RuntimeError(
                f"size mismatch for {info['title']}: {temporary.stat().st_size} != {expected_bytes}"
            )
        if _sha1(temporary) != expected_sha1:
            raise RuntimeError(f"SHA-1 mismatch for {info['title']}")
        temporary.replace(destination)
        return "downloaded"
    finally:
        temporary.unlink(missing_ok=True)


def _download_preview(
    info: dict, audio_destination: Path, backup_root: Path, dry_run: bool
) -> tuple[str, str]:
    url = str(info.get("preview_url") or "")
    if not url:
        return "unavailable-from-source", ""
    mime = str(info.get("preview_mime") or "").lower()
    suffix_by_mime = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    suffix = suffix_by_mime.get(mime)
    if not suffix:
        candidate = Path(urlparse(url).path).suffix.lower()
        suffix = candidate if candidate in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    destination = audio_destination.with_name(
        f"{audio_destination.stem} - source preview{suffix}"
    )
    if dry_run:
        return ("would-keep" if destination.exists() else "would-download"), destination.name
    request = Request(url, headers={"User-Agent": USER_AGENT})
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".part", dir=audio_destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            response_mime = str(response.headers.get_content_type() or "").lower()
            if not response_mime.startswith("image/"):
                return "unavailable-from-source", ""
            shutil.copyfileobj(response, output, length=256 * 1024)
        if destination.exists() and _sha1(destination) == _sha1(temporary):
            return "unchanged", destination.name
        if destination.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(backup_root / destination.name))
        temporary.replace(destination)
        return "downloaded", destination.name
    finally:
        temporary.unlink(missing_ok=True)


def _station_libraries(database: Path) -> dict[str, Path]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT replace(m.value, '/', '') AS mount_name, l.value AS library_folder
            FROM station_settings m
            JOIN station_settings l ON l.station_id=m.station_id
            WHERE m.key='icecast_mount' AND l.key='music_library_folder'
            """
        ).fetchall()
    finally:
        connection.close()
    return {
        str(row["mount_name"]).strip().lower(): Path(str(row["library_folder"]))
        for row in rows
        if row["mount_name"] and row["library_folder"]
    }


def _write_manifest(folder: Path, entries: list[dict], dry_run: bool) -> None:
    path = folder / MANIFEST_NAME
    previous_entries: list[dict] = []
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            previous_entries = list(previous.get("files") or [])
        except (OSError, ValueError, TypeError):
            previous_entries = []
    merged = {
        str(entry.get("local_file")): entry
        for entry in previous_entries + entries
        if isinstance(entry, dict) and entry.get("local_file")
    }
    payload = {
        "purpose": "License and source record for RadioTEDU Wikimedia Commons FLAC imports",
        "notice": "Each file remains subject to the license on its Wikimedia Commons description page.",
        "updated_unix": int(time.time()),
        "files": [merged[name] for name in sorted(merged, key=str.casefold)],
    }
    if dry_run:
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _candidate_libraries(root: Path) -> dict[str, Path]:
    resolved = root.resolve()
    if resolved.name.casefold() == "radiotedu songs":
        raise RuntimeError("Candidate destination must not be the live RadioTEDU Songs root")
    return {mount: resolved / genre for mount, genre in GENRE_FOLDERS.items()}


def _write_candidate_index(root: Path, results: list[dict], dry_run: bool) -> None:
    if dry_run:
        return
    root.mkdir(parents=True, exist_ok=True)
    readme = (
        "RADIOTEDU CREATIVE COMMONS CANDIDATES - NOT A LIVE LIBRARY\n\n"
        "Nothing in this folder is broadcast automatically. Review each file and its\n"
        "WIKIMEDIA_COMMONS_ATTRIBUTION.json record before manually copying it into\n"
        "H:\\RadioTEDU Songs\\<Genre>. Files preserve their original Commons bytes;\n"
        "license, creator, credit, source URL, SHA-1 and size are recorded beside them.\n\n"
        "A source preview image is kept when Commons supplies one. It may be cover art,\n"
        "a score page, or a generated waveform; the JSON record identifies it as a preview.\n\n"
        "RadioTEDU is non-commercial, so a Classicals.de track explicitly marked\n"
        "CC BY-NC may be eligible with attribution. This automated batch still uses\n"
        "original FLAC from Commons for quality and machine-verifiable SHA-1 records;\n"
        "review each Classicals.de track page and consent terms before manual download.\n"
    )
    (root / README_NAME).write_text(readme, encoding="utf-8")
    fields = [
        "genre", "mount", "local_file", "commons_title", "commons_page", "artist",
        "credit", "license", "license_url", "attribution_required", "source",
        "mime", "sha1", "bytes", "source_preview_file", "source_preview_status", "status",
    ]
    with (root / CATALOG_NAME).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "RadioTEDU"
        / "OnAir"
        / "cleanroom.db",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "wikimedia_commons_flac.json",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path(r"H:\RadioTEDU-OnAir-System-Backup\commons-flac-replaced"),
    )
    parser.add_argument(
        "--destination-root",
        type=Path,
        help="Write to a separate review-only genre library instead of live station folders",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    libraries = (
        _candidate_libraries(args.destination_root)
        if args.destination_root
        else _station_libraries(args.database)
    )
    backup = args.backup_root / time.strftime("%Y%m%d-%H%M%S")
    results: list[dict] = []
    for mount, titles in catalog.items():
        folder = libraries.get(mount)
        if folder is None:
            raise RuntimeError(f"No configured station library for mount /{mount}")
        station_entries: list[dict] = []
        for title in titles:
            info = _commons_info(str(title))
            destination = folder / _safe_filename(info["title"])
            status = _download(info, destination, backup / mount, args.dry_run)
            preview_status, preview_file = _download_preview(
                info, destination, backup / mount, args.dry_run
            )
            entry = {
                "genre": GENRE_FOLDERS.get(mount, mount),
                "local_file": destination.name,
                "commons_title": info["title"],
                "commons_page": info["description_url"],
                "artist": info["artist"],
                "credit": info["credit"],
                "license": info["license"],
                "license_url": info["license_url"],
                "attribution_required": info["attribution_required"],
                "source": info["source"],
                "mime": info["mime"],
                "sha1": info["sha1"],
                "bytes": info["bytes"],
                "source_preview_file": preview_file,
                "source_preview_status": preview_status,
                "source_preview_note": "Source-provided preview; may be artwork, score, or waveform",
            }
            station_entries.append(entry)
            results.append({"mount": f"/{mount}", "status": status, **entry})
        _write_manifest(folder, station_entries, args.dry_run)
    if args.destination_root:
        _write_candidate_index(args.destination_root.resolve(), results, args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
