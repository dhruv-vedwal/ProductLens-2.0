"""Authenticated encryption for durable product credentials.

Dependency-free helper that mirrors ``auth.security``: secrets are never
returned in API payloads and only decrypt at the browser boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _derive_key(secret: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), b"productlens-cred-v1", 120_000, dklen=32
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(
            hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        )
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str, secret: str) -> str:
    key = _derive_key(secret)
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data)), strict=True))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return _encode(nonce + tag + cipher)


def decrypt_secret(token: str, secret: str) -> str:
    try:
        raw = _decode(token)
        nonce, tag, cipher = raw[:16], raw[16:32], raw[32:]
        key = _derive_key(secret)
        expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("credential ciphertext failed integrity check")
        plain = bytes(
            a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher)), strict=True)
        )
        return plain.decode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ValueError("credential ciphertext is unreadable") from error
