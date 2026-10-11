"""Explicit POSIX credential provider for standalone Linux/macOS installs.

Encryption protects the vault; the local key must be backed up separately and
is protected by owner-only filesystem permissions, not Windows DPAPI.
"""
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class PortableCredentialProvider:
    PREFIX = b'RTFERNET1:'

    def __init__(self, key_path):
        self.key_path = Path(key_path).expanduser()

    def _key(self, *, create):
        path = self.key_path
        if path.is_symlink():
            raise ValueError('Credential key must not be a symbolic link')
        if not path.exists() and create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(Fernet.generate_key())
                    handle.flush()
                    os.fsync(handle.fileno())
        metadata = path.stat()
        if os.name != 'nt':
            if stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid != os.geteuid():
                raise ValueError('Credential key must be owned by this user with mode 0600')
        return path.read_bytes()

    def protect(self, data):
        return self.PREFIX + Fernet(self._key(create=True)).encrypt(data)

    def unprotect(self, data):
        if not data.startswith(self.PREFIX):
            raise ValueError('Credential blob does not use the configured portable provider')
        try:
            return Fernet(self._key(create=False)).decrypt(data[len(self.PREFIX):])
        except InvalidToken as exc:
            raise ValueError('Credential key does not match this encrypted vault') from exc
