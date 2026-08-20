"""Safely restore a missing Icecast password from the legacy Wall database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "CLEANROOM_JWT_SECRET_FILE",
    str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "RadioTEDU" / "OnAir" / "secrets" / "jwt-signing.key"),
)

from app.auth.jwt_handler import create_access_token  # noqa: E402


def _output_row(conn: sqlite3.Connection, station_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT station_id, icecast_host, icecast_port, icecast_mount, icecast_user, icecast_password "
        "FROM station_outputs WHERE station_id=?",
        (station_id,),
    ).fetchone()


def _identity(row: sqlite3.Row | None) -> tuple[str, int, str]:
    if row is None:
        return "", 0, ""
    mount = str(row["icecast_mount"] or "").strip()
    if mount and not mount.startswith("/"):
        mount = f"/{mount}"
    return (
        str(row["icecast_host"] or "").strip().lower(),
        int(row["icecast_port"] or 0),
        mount,
    )


def _api_request(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    response = requests.request(
        method,
        base_url.rstrip("/") + path,
        headers={"Authorization": "Bearer " + create_access_token(1, "admin")},
        json=payload,
        timeout=20.0,
    )
    response.raise_for_status()
    body = response.json()
    return dict(body) if isinstance(body, dict) else {"data": body}


def main() -> int:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    parser = argparse.ArgumentParser()
    parser.add_argument("station_id", type=int)
    parser.add_argument(
        "--source-db",
        type=Path,
        default=local_app_data / "RadioTEDU Broadcast Wall" / "cleanroom.db",
    )
    parser.add_argument(
        "--target-db",
        type=Path,
        default=program_data / "RadioTEDU" / "OnAir" / "cleanroom.db",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-api", action="store_true")
    parser.add_argument("--adopt-source-output", action="store_true")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8100")
    args = parser.parse_args()

    if args.apply and args.apply_api:
        parser.error("choose either --apply or --apply-api")
    if args.adopt_source_output and not args.apply_api:
        parser.error("--adopt-source-output requires --apply-api")

    if not args.source_db.is_file() or not args.target_db.is_file():
        print(json.dumps({"ok": False, "error": "database_missing"}, separators=(",", ":")))
        return 1

    with sqlite3.connect(args.source_db) as source, sqlite3.connect(args.target_db) as target:
        source_row = _output_row(source, args.station_id)
        target_row = _output_row(target, args.station_id)
        source_has_password = bool(str(source_row["icecast_password"] or "")) if source_row else False
        target_has_password = bool(str(target_row["icecast_password"] or "")) if target_row else False
        identities_match = bool(source_row and target_row and _identity(source_row) == _identity(target_row))
        result = {
            "ok": bool(source_row and target_row and source_has_password and identities_match),
            "station_id": args.station_id,
            "source_row_found": source_row is not None,
            "source_password_configured": source_has_password,
            "target_row_found": target_row is not None,
            "target_password_configured": target_has_password,
            "output_identity_matches": identities_match,
            "source_output": _identity(source_row),
            "target_output": _identity(target_row),
            "applied": False,
        }
        if args.apply_api:
            api_output = _api_request(
                args.api_base_url,
                "GET",
                f"/api/stations/output?station_id={args.station_id}",
            )
            source_identity = _identity(source_row)
            api_identity = _identity(api_output)
            same_mount = bool(source_identity[2] and source_identity[2] == api_identity[2])
            api_identity_matches = bool(source_row and source_identity == api_identity)
            result["api_output_identity_matches"] = api_identity_matches
            result["api_mount_matches"] = same_mount
            if not source_has_password or not (api_identity_matches or (args.adopt_source_output and same_mount)):
                print(json.dumps(result, separators=(",", ":")))
                return 1

            backup_dir = args.target_db.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / f"cleanroom-before-icecast-secret-api-{stamp}.db"
            with sqlite3.connect(backup_path) as backup:
                target.backup(backup)
            _api_request(
                args.api_base_url,
                "POST",
                "/api/stations/output",
                {
                    "station_id": args.station_id,
                    "local_output_enabled": bool(api_output.get("local_output_enabled")),
                    "output_device_id": str(api_output.get("output_device_id") or ""),
                    "icecast_enabled": bool(api_output.get("icecast_enabled")),
                    "icecast_host": str(
                        source_row["icecast_host"] if args.adopt_source_output else api_output.get("icecast_host") or ""
                    ),
                    "icecast_port": int(
                        source_row["icecast_port"] if args.adopt_source_output else api_output.get("icecast_port") or 0
                    ),
                    "icecast_mount": str(
                        source_row["icecast_mount"] if args.adopt_source_output else api_output.get("icecast_mount") or ""
                    ),
                    "icecast_user": str(
                        source_row["icecast_user"] if args.adopt_source_output else api_output.get("icecast_user") or "source"
                    ),
                    "icecast_password": str(source_row["icecast_password"] or ""),
                    "icecast_tls_enabled": bool(
                        int(source_row["icecast_port"] or 0) == 443
                        if args.adopt_source_output
                        else api_output.get("icecast_tls_enabled")
                    ),
                    "output_gain_db": float(api_output.get("output_gain_db") or 0.0),
                    "stream_codec_profile": str(api_output.get("stream_codec_profile") or "aac_plus_196"),
                    "stream_bitrate_kbps": int(api_output.get("stream_bitrate_kbps") or 196),
                },
            )
            confirmed = _api_request(
                args.api_base_url,
                "GET",
                f"/api/stations/output?station_id={args.station_id}",
            )
            result["applied"] = bool(confirmed.get("icecast_password_configured"))
            result["api_password_configured"] = bool(confirmed.get("icecast_password_configured"))
            result["backup_path"] = str(backup_path)
            print(json.dumps(result, separators=(",", ":")))
            return 0 if result["applied"] else 1
        if not args.apply:
            print(json.dumps(result, separators=(",", ":")))
            return 0 if result["ok"] else 1
        if not result["ok"]:
            print(json.dumps(result, separators=(",", ":")))
            return 1

        backup_dir = args.target_db.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"cleanroom-before-icecast-secret-{stamp}.db"
        with sqlite3.connect(backup_path) as backup:
            target.backup(backup)
        target.execute(
            "UPDATE station_outputs SET icecast_password=? WHERE station_id=?",
            (str(source_row["icecast_password"] or ""), args.station_id),
        )
        target.commit()
        result["target_password_configured"] = True
        result["applied"] = True
        result["backup_path"] = str(backup_path)
        print(json.dumps(result, separators=(",", ":")))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
