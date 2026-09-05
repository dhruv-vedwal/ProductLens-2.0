"""Small, dependency-free password and signed-session helpers.

Passwords are deliberately never stored or returned. Tokens are signed, expire,
and are additionally backed by a revocable server-side session row.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

_ITERATIONS = 600_000


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _decode(salt), int(iterations)
        )
        return hmac.compare_digest(actual, _decode(expected))
    except (TypeError, ValueError):
        return False


def create_access_token(*, user_id: str, session_id: str, secret: str, ttl_seconds: int) -> str:
    payload = {"sub": user_id, "sid": session_id, "exp": int(time.time()) + ttl_seconds, "typ": "access"}
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def decode_access_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        encoded, signature = token.split(".", 1)
        expected = _encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_decode(encoded))
        if payload.get("typ") != "access" or not isinstance(payload.get("sub"), str) or not isinstance(payload.get("sid"), str):
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
