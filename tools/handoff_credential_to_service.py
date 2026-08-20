"""Hand an existing DPAPI credential to the running broadcast service.

This is a transition helper for installations where a shared vault was first
written by the interactive operator but broadcasting now runs as LocalSystem.
The secret is decrypted in memory and sent only to the authenticated loopback
API. Neither the credential nor the API request body is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    parser = argparse.ArgumentParser()
    parser.add_argument("station_id", type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument(
        "--db",
        type=Path,
        default=program_data / "RadioTEDU" / "OnAir" / "cleanroom.db",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=program_data
        / "RadioTEDU"
        / "OnAir"
        / "secrets"
        / "station-credentials.json",
    )
    parser.add_argument(
        "--jwt-secret-file",
        type=Path,
        default=local_app_data
        / "RadioTEDU"
        / "OnAir"
        / "secrets"
        / "jwt-signing.key",
    )
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve()
    vault_path = args.vault.expanduser().resolve()
    jwt_path = args.jwt_secret_file.expanduser().resolve()
    if not db_path.is_file() or not vault_path.is_file() or not jwt_path.is_file():
        print(json.dumps({"ok": False, "error": "required_file_missing"}))
        return 1

    os.environ["CLEANROOM_JWT_SECRET_FILE"] = str(jwt_path)
    os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(vault_path)

    from app.auth.jwt_handler import create_access_token
    from app.security.credential_vault import CredentialVault, is_credential_reference

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM station_outputs WHERE station_id=?",
            (args.station_id,),
        ).fetchone()
        settings = {
            str(item["key"]): str(item["value"] or "")
            for item in conn.execute(
                "SELECT key, value FROM station_settings WHERE station_id=?",
                (args.station_id,),
            ).fetchall()
        }
    if row is None:
        print(json.dumps({"ok": False, "error": "station_output_missing"}))
        return 1

    reference = str(row["icecast_password"] or "")
    if not is_credential_reference(reference):
        print(json.dumps({"ok": False, "error": "credential_reference_missing"}))
        return 1

    # Supplying a custom protector disables automatic scope migration in this
    # read-only caller. The service is the only process allowed to rewrite the
    # SYSTEM-owned shared vault.
    vault = CredentialVault(vault_path, protect=lambda value: value)
    password = vault.get_secret(reference)
    if not password:
        print(json.dumps({"ok": False, "error": "credential_empty"}))
        return 1

    payload = {
        "station_id": args.station_id,
        "local_output_enabled": bool(row["local_output_enabled"]),
        "output_device_id": str(row["output_device_id"] or ""),
        "icecast_enabled": bool(row["icecast_enabled"]),
        "icecast_host": str(row["icecast_host"] or ""),
        "icecast_port": int(row["icecast_port"] or 0),
        "icecast_mount": str(row["icecast_mount"] or ""),
        "icecast_user": str(row["icecast_user"] or "source"),
        "icecast_password": password,
        "icecast_tls_enabled": _truthy(
            settings.get("icecast_tls_enabled", int(row["icecast_port"] or 0) == 443)
        ),
        "output_gain_db": float(row["output_gain_db"] or 0.0),
        "stream_codec_profile": str(row["stream_codec_profile"] or "aac_plus_196"),
        "stream_bitrate_kbps": int(row["stream_bitrate_kbps"] or 196),
    }
    headers = {"Authorization": "Bearer " + create_access_token(1, "admin")}
    response = requests.post(
        args.base_url.rstrip("/") + "/api/stations/output",
        headers=headers,
        json=payload,
        timeout=20.0,
    )
    password = ""
    payload["icecast_password"] = ""
    if not response.ok:
        detail = " ".join(str(response.text or "").split())[:300]
        print(
            json.dumps(
                {"ok": False, "error": "service_handoff_failed", "status": response.status_code, "detail": detail},
                separators=(",", ":"),
            )
        )
        return 1

    confirmed = requests.get(
        args.base_url.rstrip("/")
        + f"/api/stations/output?station_id={args.station_id}",
        headers=headers,
        timeout=20.0,
    )
    confirmed.raise_for_status()
    body = confirmed.json()
    configured = bool(body.get("icecast_password_configured"))
    print(
        json.dumps(
            {
                "ok": configured,
                "station_id": args.station_id,
                "service_handoff_applied": True,
                "service_can_decrypt": configured,
            },
            separators=(",", ":"),
        )
    )
    return 0 if configured else 1


if __name__ == "__main__":
    raise SystemExit(main())
