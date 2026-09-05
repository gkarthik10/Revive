"""
Revive - Auth / Security

Password hashing and JWT helpers.

Deliberately dependency-light: password hashing uses PBKDF2-HMAC-SHA256
from Python's stdlib `hashlib` (no bcrypt/passlib, no C extension to
compile), and JWTs use PyJWT, which is added to requirements.txt.

SECRET KEY
----------
Set REVIVE_JWT_SECRET in the environment (revive/backend/.env or
revive/.env) before running in anything other than local dev. If it's
missing, a fixed dev-only fallback is used and a warning is printed on
import so it's impossible to miss in the server logs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

# ------------------------------------------------------------------
# JWT configuration
# ------------------------------------------------------------------

_DEV_FALLBACK_SECRET = "revive-dev-only-secret-change-me"

JWT_SECRET = os.environ.get("REVIVE_JWT_SECRET", _DEV_FALLBACK_SECRET)
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.environ.get("REVIVE_JWT_EXPIRE_MINUTES", "480")  # 8 hours
)

if JWT_SECRET == _DEV_FALLBACK_SECRET:
    print(
        "[auth] WARNING: REVIVE_JWT_SECRET is not set — using an "
        "insecure development fallback. Set REVIVE_JWT_SECRET in "
        "revive/backend/.env before deploying this anywhere real."
    )


def create_access_token(payload: dict[str, Any]) -> str:
    """
    Mint a signed JWT for the given payload (typically {"sub": user_id,
    "email": ..., "role": ...}). Adds standard "iat"/"exp" claims.
    """

    now = datetime.now(timezone.utc)

    to_encode = {
        **payload,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }

    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """
    Verify and decode a JWT. Raises jwt.PyJWTError (or a subclass) on
    any problem — expired, malformed, bad signature, etc. Callers
    translate that into an HTTP 401.
    """

    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


# ------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256, stdlib only)
# ------------------------------------------------------------------

_PBKDF2_ITERATIONS = 260_000


def hash_password(plain_password: str) -> str:
    """
    Returns "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>".
    Storing the iteration count alongside the hash means we can raise
    it later without invalidating passwords hashed under the old count.
    """

    salt = secrets.token_hex(16)

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        bytes.fromhex(salt),
        _PBKDF2_ITERATIONS,
    )

    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${derived.hex()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """
    Constant-time comparison against a hash produced by hash_password.
    Returns False (never raises) on a malformed stored_hash so a
    corrupted users.json entry can't be leveraged into a bypass.
    """

    try:
        scheme, iterations_str, salt, expected_hex = stored_hash.split("$")

        if scheme != "pbkdf2_sha256":
            return False

        iterations = int(iterations_str)

        derived = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
        )

        return hmac.compare_digest(derived.hex(), expected_hex)
    except (ValueError, AttributeError):
        return False
