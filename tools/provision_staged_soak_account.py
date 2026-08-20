from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{label} must be an absolute path")
    return path.resolve(strict=False)


def _protect_windows_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    domain = str(os.getenv("USERDOMAIN") or "").strip()
    username = getpass.getuser()
    account = f"{domain}\\{username}" if domain else username
    completed = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{account}:(F)",
            "SYSTEM:(F)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit("could not protect the soak verifier password file")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision the staged RadioTEDU soak verifier without printing its password."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--user-root", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:18110")
    parser.add_argument("--username", default="soak-verifier")
    args = parser.parse_args()

    database = _absolute_path(args.database, "database")
    data_root = _absolute_path(args.data_root, "data root")
    user_root = _absolute_path(args.user_root, "user root")
    if not database.is_file():
        raise SystemExit("staged database is missing")
    if not data_root.is_dir() or not user_root.is_dir():
        raise SystemExit("staged data or user root is missing")

    os.environ["CLEANROOM_DB_PATH"] = str(database)
    os.environ["CLEANROOM_DATA_ROOT"] = str(data_root)
    os.environ["CLEANROOM_USER_CONFIG_ROOT"] = str(user_root)
    os.environ["CLEANROOM_JWT_SECRET_FILE"] = str(
        user_root / "secrets" / "jwt-signing.key"
    )
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    from app.auth.jwt_handler import create_access_token

    connection = sqlite3.connect(database)
    try:
        admin = connection.execute(
            "SELECT id, role FROM users WHERE is_active=1 AND role='admin' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        verifier = connection.execute(
            "SELECT id FROM users WHERE is_active=1 AND username=?",
            (str(args.username),),
        ).fetchone()
    finally:
        connection.close()
    if admin is None:
        raise SystemExit("no active staged administrator exists")
    if verifier is None:
        raise SystemExit("the staged soak-verifier account does not exist")

    password = secrets.token_urlsafe(36)
    token = create_access_token(int(admin[0]), str(admin[1]))
    request = urllib.request.Request(
        f"{str(args.api_base).rstrip('/')}/api/users/{int(verifier[0])}/reset-password",
        data=json.dumps({"new_password": password}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if int(response.status) != 200:
            raise SystemExit("soak-verifier password reset failed")

    secret_dir = user_root / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    target = secret_dir / "soak-verifier-password.txt"
    temporary = secret_dir / f".{target.name}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_text(password + "\n", encoding="utf-8")
        _protect_windows_file(temporary)
        os.replace(temporary, target)
        _protect_windows_file(target)
    finally:
        password = ""
        if temporary.exists():
            temporary.unlink()

    print(
        json.dumps(
            {
                "configured": True,
                "username": str(args.username),
                "password_file": str(target),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
