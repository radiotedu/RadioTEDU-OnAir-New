from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
DATABASE_PATH = Path(r"C:\ProgramData\RadioTEDU\OnAir\cleanroom.db")
LIBRARY_ROOT = Path(r"H:\RadioTEDU Songs")
GLOBAL_AD_FOLDER = LIBRARY_ROOT / "Ads" / "Ads"
JINGLE_ROOT = LIBRARY_ROOT / "Ads" / "Jingles"
BACKUP_ROOT = Path(r"H:\RadioTEDU-Backups")


@dataclass(frozen=True)
class StationProfile:
    station_id: int
    label: str
    genre: str
    music_folder: Path
    jingle_folder: Path
    processing_profile: str


PROFILES = (
    StationProfile(1, "Classical", "Classical", LIBRARY_ROOT / "Classical", JINGLE_ROOT / "Classical", "classical"),
    StationProfile(2, "Lo-Fi", "Lo-Fi", LIBRARY_ROOT / "lofi", JINGLE_ROOT / "Lofi", "lofi"),
    StationProfile(4, "Pop / Radio", "Pop", LIBRARY_ROOT / "Pop", JINGLE_ROOT / "Pop", "pop"),
    StationProfile(5, "Jazz", "Jazz", LIBRARY_ROOT / "Jazz", JINGLE_ROOT / "Jazz", "jazz"),
    StationProfile(8, "Rock", "Rock", LIBRARY_ROOT / "Rock", JINGLE_ROOT / "Rock", "rock"),
    StationProfile(9, "Energize", "Electronic", LIBRARY_ROOT / "Energize", JINGLE_ROOT / "Energize", "energize"),
)


def _audio_count(folder: Path) -> int:
    extensions = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
    return sum(1 for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in extensions)


def _backup_database() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination_dir = BACKUP_ROOT / f"{stamp}-live-folder-commission"
    destination_dir.mkdir(parents=True, exist_ok=False)
    destination = destination_dir / "cleanroom.db"
    with sqlite3.connect(DATABASE_PATH) as source, sqlite3.connect(destination) as target:
        source.backup(target)
        integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity.lower() != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return destination


def _selected_profiles(station_ids: list[int]) -> list[StationProfile]:
    selected = set(station_ids)
    profiles = [profile for profile in PROFILES if not selected or profile.station_id in selected]
    unknown = selected - {profile.station_id for profile in PROFILES}
    if unknown:
        raise ValueError(f"unsupported station ids: {sorted(unknown)}")
    return profiles


def _set_profile_settings(conn, profile: StationProfile) -> None:
    values = {
        "music_library_folder": str(profile.music_folder.resolve()),
        "library_management_mode": "replace",
        "library_recursive": "true",
        "library_profile_label": profile.label,
        "library_default_genre": profile.genre,
        "library_skip_unplayable": "true",
        "library_rescan_interval_seconds": "300",
        "jingle_library_folder": str(profile.jingle_folder.resolve()),
        "jingle_folder": str(profile.jingle_folder.resolve()),
        "jingle_library_management_mode": "replace",
        "jingle_library_recursive": "true",
        "jingle_library_profile_label": f"{profile.label} English jingles",
        "jingle_library_skip_unplayable": "true",
        "jingle_library_rescan_interval_seconds": "300",
        "ad_library_folder": str(GLOBAL_AD_FOLDER.resolve()),
        "hourly_ad_folder": str(GLOBAL_AD_FOLDER.resolve()),
        "ad_library_management_mode": "replace",
        "ad_library_recursive": "true",
        "ad_library_profile_label": "Global ads",
        "ad_library_skip_unplayable": "true",
        "ad_library_rescan_interval_seconds": "300",
        "sweeper_folder_autofollow": "true",
        # A station with no matching jingle files must not schedule a jingle.
        # Autofollow will turn this on after a valid file appears on a later
        # managed-folder sync.
        "sweeper_enabled": "true" if _audio_count(profile.jingle_folder) > 0 else "false",
        "sweeper_interval": "3",
        "sweeper_interval_unit": "tracks",
        "sweeper_mode": "ordered",
        "broadcast_processing_profile": profile.processing_profile,
        # These six local music stations are deliberately non-AI. Keeping the
        # values explicit prevents an old UI profile from re-enabling intro
        # generation and consuming CPU needed by the source encoders.
        "ai_host_enabled": "false",
        "ai_include_music_history": "false",
        "campaign_ai_only": "false",
        "startup_ai_readiness_state": "disabled",
        "startup_ai_ready_intro_count": "0",
        "startup_ai_required_intro_count": "0",
    }
    for key, value in values.items():
        conn.execute(
            "INSERT INTO station_settings (station_id, key, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(station_id, key) DO UPDATE SET "
            "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (profile.station_id, key, value),
        )


def _set_flac_enabled(conn, profile: StationProfile, enabled: bool) -> None:
    key = f"station_{profile.station_id}_extra_icecast_outputs"
    row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    outputs = json.loads(str(row["value"] or "[]")) if row else []
    found = False
    for output in outputs:
        quality = str(output.get("quality", "")).strip().lower()
        output["enabled"] = bool(enabled and quality == "flac")
        if quality == "flac":
            found = True
            output["icecast_public"] = True
            output["metadata_suppressed"] = False
    if not found:
        raise RuntimeError(f"station {profile.station_id} has no persisted FLAC output")
    serialized = json.dumps(outputs, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        "INSERT INTO system_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, serialized),
    )


def _sync_profile(profile: StationProfile) -> list[dict]:
    from app.api.legacy import LibraryFolderSyncPayload, sync_station_library_folder

    results = []
    for track_type, folder, label, genre in (
        ("music", profile.music_folder, profile.label, profile.genre),
        ("jingle", profile.jingle_folder, f"{profile.label} English jingles", ""),
        ("ad", GLOBAL_AD_FOLDER, "Global ads", ""),
    ):
        results.append(
            sync_station_library_folder(
                LibraryFolderSyncPayload(
                    station_id=profile.station_id,
                    folder=str(folder),
                    recursive=True,
                    track_type=track_type,
                    mode="replace",
                    skip_unplayable=True,
                    profile_label=label,
                    default_genre=genre,
                    incremental=True,
                    allow_empty=True,
                )
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Commission RadioTEDU live-folder playout safely.")
    parser.add_argument("--apply", action="store_true", help="Persist and synchronize the configuration")
    parser.add_argument("--enable-flac", action="store_true", help="Enable only the persisted -flac variant")
    parser.add_argument("--refresh-metadata", action="store_true", help="Reprobe managed tags and artwork once")
    parser.add_argument("--accept-current-metadata", action="store_true", help="Trust current DB tags and backfill file signatures")
    parser.add_argument("--skip-sync", action="store_true", help="Persist settings without running a folder sync")
    parser.add_argument("--station-id", action="append", type=int, default=[], help="Limit to one or more station ids")
    args = parser.parse_args()
    profiles = _selected_profiles(args.station_id)

    missing = [str(path) for profile in profiles for path in (profile.music_folder, profile.jingle_folder) if not path.is_dir()]
    if not GLOBAL_AD_FOLDER.is_dir():
        missing.append(str(GLOBAL_AD_FOLDER))
    if missing:
        raise FileNotFoundError("missing managed folders: " + ", ".join(sorted(set(missing))))

    inventory = [
        {
            "station_id": profile.station_id,
            "label": profile.label,
            "music_files": _audio_count(profile.music_folder),
            "jingle_files": _audio_count(profile.jingle_folder),
            "ad_files": _audio_count(GLOBAL_AD_FOLDER),
        }
        for profile in profiles
    ]
    if not args.apply:
        print(json.dumps({"apply": False, "inventory": inventory}, ensure_ascii=False, indent=2))
        return 0

    backup_path = _backup_database()

    os.environ["CLEANROOM_DB_PATH"] = str(DATABASE_PATH)
    os.chdir(REPOSITORY_ROOT)
    from app.db import get_connection, init_db
    from app.engine.broadcast_queue_autofill import reconcile_pending_sweeper_queue

    init_db()
    conn = get_connection()
    try:
        for profile in profiles:
            _set_profile_settings(conn, profile)
            _set_flac_enabled(conn, profile, bool(args.enable_flac))
            if args.refresh_metadata:
                conn.execute(
                    "UPDATE tracks SET managed_file_size=-1, managed_file_mtime_ns=-1 "
                    "WHERE station_id=? AND is_active=1 AND LOWER(track_type) IN ('music','jingle','ad')",
                    (profile.station_id,),
                )
            if args.accept_current_metadata:
                rows = conn.execute(
                    "SELECT id, file_path FROM tracks WHERE station_id=? AND is_active=1 "
                    "AND LOWER(track_type) IN ('music','jingle','ad')",
                    (profile.station_id,),
                ).fetchall()
                for row in rows:
                    try:
                        stat = Path(str(row["file_path"] or "")).stat()
                    except OSError:
                        continue
                    conn.execute(
                        "UPDATE tracks SET managed_file_size=?, managed_file_mtime_ns=? WHERE id=?",
                        (int(stat.st_size), int(stat.st_mtime_ns), int(row["id"])),
                    )
        conn.commit()
    finally:
        conn.close()

    if args.skip_sync:
        print(json.dumps({"apply": True, "backup": str(backup_path), "sync": "skipped", "inventory": inventory}, ensure_ascii=False, indent=2))
        return 0

    sync_results = []
    for profile in profiles:
        sync_results.extend(_sync_profile(profile))
        conn = get_connection()
        try:
            reconcile_pending_sweeper_queue(conn, profile.station_id)
            conn.commit()
        finally:
            conn.close()

    print(
        json.dumps(
            {
                "apply": True,
                "backup": str(backup_path),
                "flac_enabled": bool(args.enable_flac),
                "inventory": inventory,
                "sync": [
                    {
                        "station_id": result["station_id"],
                        "track_type": result["track_type"],
                        "active_files": result["active_files"],
                        "added": result["added"],
                        "deactivated": result["deactivated"],
                        "metadata_probed": result["metadata_probed"],
                    }
                    for result in sync_results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
