"""Small authenticated-encryption format for portable recovery secrets."""

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


MAGIC = b"RTREC1\x00"
SALT_BYTES = 16
NONCE_BYTES = 12
PBKDF2_ITERATIONS = 600_000


def _derive_key(password: str, salt: bytes) -> bytes:
    if not str(password):
        raise ValueError("A recovery password is required")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    ).derive(str(password).encode("utf-8"))


def encrypt_json(payload: dict[str, Any], password: str) -> bytes:
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = AESGCM(_derive_key(password, salt)).encrypt(nonce, plaintext, MAGIC)
    return MAGIC + salt + nonce + encrypted


def decrypt_json(blob: bytes, password: str) -> dict[str, Any]:
    if not blob.startswith(MAGIC):
        raise ValueError("Unsupported RadioTEDU recovery secret format")
    offset = len(MAGIC)
    minimum = offset + SALT_BYTES + NONCE_BYTES + 16
    if len(blob) < minimum:
        raise ValueError("Truncated RadioTEDU recovery secret payload")
    salt = blob[offset : offset + SALT_BYTES]
    offset += SALT_BYTES
    nonce = blob[offset : offset + NONCE_BYTES]
    encrypted = blob[offset + NONCE_BYTES :]
    plaintext = AESGCM(_derive_key(password, salt)).decrypt(nonce, encrypted, MAGIC)
    payload = json.loads(plaintext.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RadioTEDU recovery secret payload is invalid")
    return payload
