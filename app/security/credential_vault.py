from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from pathlib import Path
from typing import Callable

from app.config import get_user_config_root

_REFERENCE_PREFIX = "credential://user/"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_CRYPTPROTECT_LOCAL_MACHINE = 0x04
_DPAPI_SCOPE_ENV = "CLEANROOM_CREDENTIAL_DPAPI_SCOPE"
_COMPAT_DPAPI_SCOPE_ENV = ""


class CredentialVaultError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _windows_protect(data: bytes, *, machine_scope: bool = False) -> bytes:
    input_blob, input_buffer = _input_blob(data)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    flags = _CRYPTPROTECT_UI_FORBIDDEN
    if machine_scope:
        flags |= _CRYPTPROTECT_LOCAL_MACHINE
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "RadioTEDU OnAir credential",
        None,
        None,
        None,
        flags,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        raise CredentialVaultError(
            f"Windows credential protection failed with error {ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _windows_unprotect(data: bytes) -> bytes:
    input_blob, input_buffer = _input_blob(data)
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        raise CredentialVaultError(
            f"Windows credential decryption failed with error {ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _default_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialVaultError(
            "OS credential protection is unavailable on this platform; "
            "configure a supported credential provider"
        )
    return _windows_protect(data)


def _machine_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialVaultError(
            "OS credential protection is unavailable on this platform; "
            "configure a supported credential provider"
        )
    return _windows_protect(data, machine_scope=True)


def _default_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialVaultError(
            "OS credential protection is unavailable on this platform; "
            "configure a supported credential provider"
        )
    return _windows_unprotect(data)


def credential_protection_scope(path: str | Path) -> str:
    """Return the DPAPI scope appropriate for a credential store path.

    A ProgramData vault is shared with the LocalSystem continuity service, so
    its encrypted values must use machine-scoped DPAPI. Vaults elsewhere stay
    bound to the interactive Windows user. The explicit environment override
    supports unusual managed deployments without weakening the default.
    """

    configured = (
        os.getenv(_DPAPI_SCOPE_ENV, "").strip().lower()
        or ""
    )
    if configured:
        if configured not in {"user", "machine"}:
            raise CredentialVaultError(
                f"{_DPAPI_SCOPE_ENV} must be either 'user' or 'machine'"
            )
        return configured

    program_data = os.getenv("PROGRAMDATA", "").strip()
    if os.name == "nt" and program_data:
        candidate = Path(path).expanduser().resolve()
        shared_roots = (
            (Path(program_data).expanduser().resolve() / "RadioTEDU" / "OnAir").resolve(),
        )
        for shared_root in shared_roots:
            try:
                candidate.relative_to(shared_root)
            except ValueError:
                continue
            return "machine"
    return "user"


def credential_reference(station_id: int) -> str:
    return f"{_REFERENCE_PREFIX}station/{int(station_id)}/icecast"


def system_credential_reference(name: str) -> str:
    normalized = str(name or "").strip().lower().replace("_", "-")
    if not normalized or any(
        token not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for token in normalized
    ):
        raise CredentialVaultError("Invalid system credential name")
    return f"{_REFERENCE_PREFIX}system/{normalized}"


def is_credential_reference(value: str) -> bool:
    return str(value or "").startswith(_REFERENCE_PREFIX)


class CredentialVault:
    """DPAPI-protected credential storage with atomic updates.

    Per-user vaults use user-scoped DPAPI. The ACL-restricted shared ProgramData
    vault uses machine-scoped DPAPI so the LocalSystem continuity service and
    the interactive operator can resolve the same broadcast credential.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ):
        configured = (
            os.getenv("CLEANROOM_CREDENTIAL_STORE_FILE", "").strip()
        )
        self.path = (
            Path(path).expanduser().resolve()
            if path is not None
            else (
                Path(configured).expanduser().resolve()
                if configured
                else (
                    get_user_config_root()
                    / "secrets"
                    / "station-credentials.json"
                ).resolve()
            )
        )
        self.protection_scope = (
            "custom" if protect is not None else credential_protection_scope(self.path)
        )
        self._protect = (
            protect
            if protect is not None
            else (
                _machine_protect
                if self.protection_scope == "machine"
                else _default_protect
            )
        )
        self._unprotect = unprotect or _default_unprotect
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "scope": "", "credentials": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise CredentialVaultError(
                f"Credential store could not be read: {self.path}"
            ) from exc
        if int(payload.get("version", 0) or 0) != 1:
            raise CredentialVaultError("Unsupported credential store version")
        credentials = payload.get("credentials")
        if not isinstance(credentials, dict):
            raise CredentialVaultError("Credential store payload is invalid")
        return {
            "version": 1,
            "scope": str(payload.get("scope") or "").strip().lower(),
            "credentials": dict(credentials),
        }

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except OSError as exc:
            raise CredentialVaultError(
                f"Credential store could not be updated: {self.path}"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def set_secret(self, reference: str, secret: str) -> None:
        normalized_reference = str(reference or "").strip()
        normalized_secret = str(secret or "")
        if not is_credential_reference(normalized_reference):
            raise CredentialVaultError("Invalid credential reference")
        if not normalized_secret:
            raise CredentialVaultError("Refusing to store an empty credential")
        protected = self._protect(normalized_secret.encode("utf-8"))
        encoded = base64.b64encode(protected).decode("ascii")
        with self._lock:
            payload = self._read()
            payload["credentials"][normalized_reference] = encoded
            if self.protection_scope in {"user", "machine"}:
                payload["scope"] = self.protection_scope
            self._write(payload)

    def get_secret(self, reference: str) -> str:
        normalized_reference = str(reference or "").strip()
        if not is_credential_reference(normalized_reference):
            return ""
        with self._lock:
            payload = self._read()
            encoded = payload["credentials"].get(normalized_reference)
            if not encoded:
                return ""
            try:
                protected = base64.b64decode(str(encoded), validate=True)
                plaintext = self._unprotect(protected)
                result = plaintext.decode("utf-8")
                # Older vaults did not record their DPAPI scope. After the
                # account that owns that legacy blob successfully decrypts it,
                # migrate the complete vault once to the configured scope.
                if (
                    self.protection_scope in {"user", "machine"}
                    and payload.get("scope") != self.protection_scope
                ):
                    self._rewrap_payload(payload)
                return result
            except (ValueError, UnicodeError, OSError) as exc:
                raise CredentialVaultError(
                    f"Credential could not be decrypted: {normalized_reference}"
                ) from exc

    def has_secret(self, reference: str) -> bool:
        normalized_reference = str(reference or "").strip()
        if not is_credential_reference(normalized_reference):
            return False
        with self._lock:
            return bool(self._read()["credentials"].get(normalized_reference))

    def delete_secret(self, reference: str) -> None:
        normalized_reference = str(reference or "").strip()
        with self._lock:
            payload = self._read()
            if payload["credentials"].pop(normalized_reference, None) is not None:
                self._write(payload)

    def export_secrets(self, references: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
        """Decrypt selected entries for immediate use by an encrypted export."""
        with self._lock:
            payload = self._read()
            available = tuple(str(item) for item in payload["credentials"])
        selected = available if references is None else tuple(dict.fromkeys(references))
        exported: dict[str, str] = {}
        for reference in selected:
            if not is_credential_reference(reference):
                raise CredentialVaultError("Invalid credential reference")
            value = self.get_secret(reference)
            if value:
                exported[reference] = value
        return exported

    def import_secrets(self, values: dict[str, str]) -> int:
        """Protect portable recovery values with this host's configured scope."""
        if not isinstance(values, dict):
            raise CredentialVaultError("Credential import payload is invalid")
        normalized: dict[str, str] = {}
        for reference, secret in values.items():
            ref = str(reference or "").strip()
            value = str(secret or "")
            if not is_credential_reference(ref) or not value:
                raise CredentialVaultError("Credential import payload is invalid")
            normalized[ref] = value

        # Protect every value before touching the on-disk payload. A recovery
        # import either replaces all requested entries in one atomic write or
        # leaves the existing vault byte-for-byte unchanged.
        protected = {
            reference: base64.b64encode(self._protect(secret.encode("utf-8"))).decode(
                "ascii"
            )
            for reference, secret in normalized.items()
        }
        with self._lock:
            payload = self._read()
            payload["credentials"].update(protected)
            if self.protection_scope in {"user", "machine"}:
                payload["scope"] = self.protection_scope
            if protected:
                self._write(payload)
        return len(normalized)

    def rewrap_for_configured_scope(self) -> int:
        """Atomically re-encrypt every entry with this vault's current scope."""

        with self._lock:
            payload = self._read()
            return self._rewrap_payload(payload)

    def _rewrap_payload(self, payload: dict) -> int:
        rewrapped: dict[str, str] = {}
        for reference, encoded in payload["credentials"].items():
            try:
                protected = base64.b64decode(str(encoded), validate=True)
                plaintext = self._unprotect(protected)
                rewrapped[str(reference)] = base64.b64encode(
                    self._protect(plaintext)
                ).decode("ascii")
            except (ValueError, UnicodeError, OSError) as exc:
                raise CredentialVaultError(
                    f"Credential could not be rewrapped: {reference}"
                ) from exc
        payload["credentials"] = rewrapped
        if self.protection_scope in {"user", "machine"}:
            payload["scope"] = self.protection_scope
        self._write(payload)
        return len(rewrapped)


def get_credential_vault() -> CredentialVault:
    # Resolve the configured path for every operation so isolated tests and
    # multi-profile launches cannot retain a stale vault singleton.
    return CredentialVault()


def store_station_icecast_password(station_id: int, password: str) -> str:
    reference = credential_reference(station_id)
    get_credential_vault().set_secret(reference, password)
    return reference


def store_system_secret(name: str, secret: str) -> str:
    reference = system_credential_reference(name)
    get_credential_vault().set_secret(reference, secret)
    return reference


def resolve_credential_value(stored_value: str) -> str:
    value = str(stored_value or "")
    if not is_credential_reference(value):
        return value
    return get_credential_vault().get_secret(value)


def resolve_station_icecast_password(station_id: int, stored_value: str) -> str:
    value = str(stored_value or "")
    # Legacy plaintext remains readable only long enough for a
    # non-destructive migration on the next successful save/startup.
    return resolve_credential_value(value)


def protect_data(data: bytes) -> bytes:
    """Protect arbitrary local recovery data with the configured OS facility."""
    return _default_protect(bytes(data))


def unprotect_data(data: bytes) -> bytes:
    return _default_unprotect(bytes(data))
