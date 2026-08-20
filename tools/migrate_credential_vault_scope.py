"""Rewrap a credential vault for the Windows account that runs broadcasting.

The command never prints decrypted values. Without ``--apply`` it only proves
that every entry can be decrypted by the caller. Applying creates a timestamped
backup before atomically re-encrypting all entries with the requested DPAPI
scope.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vault",
        type=Path,
        default=program_data
        / "RadioTEDU"
        / "OnAir"
        / "secrets"
        / "station-credentials.json",
    )
    parser.add_argument("--scope", choices=("user", "machine"), default="machine")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    vault_path = args.vault.expanduser().resolve()
    if not vault_path.is_file():
        print(json.dumps({"ok": False, "error": "vault_missing", "path": str(vault_path)}))
        return 1

    os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(vault_path)
    os.environ["CLEANROOM_CREDENTIAL_DPAPI_SCOPE"] = args.scope

    from app.security.credential_vault import CredentialVault, CredentialVaultError

    vault = CredentialVault(vault_path)
    try:
        payload = vault._read()
        references = tuple(str(value) for value in payload["credentials"])
        for reference in references:
            if not vault.get_secret(reference):
                raise CredentialVaultError(f"Credential is empty: {reference}")
    except CredentialVaultError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "credential_not_decryptable",
                    "detail": str(exc),
                    "path": str(vault_path),
                },
                separators=(",", ":"),
            )
        )
        return 1

    result = {
        "ok": True,
        "path": str(vault_path),
        "credential_count": len(references),
        "target_scope": vault.protection_scope,
        "decryptable_by_caller": True,
        "applied": False,
    }
    if not args.apply:
        print(json.dumps(result, separators=(",", ":")))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = vault_path.parents[1] / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"station-credentials-before-dpapi-{args.scope}-{stamp}.json"
    shutil.copy2(vault_path, backup_path)
    original_bytes = vault_path.read_bytes()

    try:
        migrated = vault.rewrap_for_configured_scope()
        for reference in references:
            if not vault.get_secret(reference):
                raise CredentialVaultError(f"Rewrapped credential is empty: {reference}")
    except Exception:
        # Do not attempt a privileged restore when the initial atomic write was
        # rejected before changing the file. If bytes did change, make the
        # restore failure explicit instead of hiding the original state.
        if vault_path.read_bytes() != original_bytes:
            shutil.copy2(backup_path, vault_path)
        raise

    result.update(
        {
            "credential_count": migrated,
            "applied": True,
            "backup_path": str(backup_path),
            "verified_after_write": True,
        }
    )
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
