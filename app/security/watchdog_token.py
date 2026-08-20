from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
from pathlib import Path

from fastapi import Request

from app.config import get_data_root


WATCHDOG_HEADER = "x-radiotedu-watchdog-token"


def watchdog_token_path() -> Path:
    return get_data_root() / "secrets" / "watchdog-api.key"


def ensure_watchdog_token() -> str:
    path = watchdog_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeError("watchdog_token_invalid")
        return token
    token = secrets.token_urlsafe(48)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(token + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token
    except Exception:
        path.unlink(missing_ok=True)
        raise


def request_is_loopback(request: Request) -> bool:
    host = str(request.client.host if request.client else "").strip()
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def watchdog_request_is_valid(request: Request) -> bool:
    if not request_is_loopback(request):
        return False
    supplied = str(request.headers.get(WATCHDOG_HEADER, "") or "").strip()
    if not supplied:
        # Creating the file on the first local request lets a newly installed
        # scheduled task discover the token without an installer-side secret.
        ensure_watchdog_token()
        return False
    expected = ensure_watchdog_token()
    return hmac.compare_digest(supplied, expected)
