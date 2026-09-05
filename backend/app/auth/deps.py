"""
Revive - Auth / FastAPI dependencies.
"""

from __future__ import annotations

import jwt
from fastapi import Header, HTTPException

from app.auth.security import decode_access_token
from app.auth.store import get_user_by_id, normalize_role


def _extract_token(
    authorization: str | None,
) -> str:
    if (
        not authorization
        or not authorization.startswith("Bearer ")
    ):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header.",
        )

    token = authorization.removeprefix(
        "Bearer "
    ).strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token.",
        )

    return token


def get_current_user(
    authorization: str | None = Header(default=None),
) -> dict:
    token = _extract_token(
        authorization
    )

    try:
        claims = decode_access_token(
            token
        )

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Session expired. Please sign in again.",
        )

    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid session token.",
        )

    user = get_user_by_id(
        claims.get("sub", "")
    )

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Account no longer exists.",
        )

    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": normalize_role(
            user.get("role")
        ),
        "created_at": user.get(
            "created_at"
        ),
    }


def require_admin(
    user: dict = None,
):
    """
    Optional helper for route handlers that need explicit
    administrator authorization.

    Most team routes use this at the API layer.
    """

    if not user or user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Administrator access is required.",
        )

    return user


def require_operator_or_admin(
    user: dict = None,
):
    if not user or user.get("role") not in {
        "admin",
        "operator",
    }:
        raise HTTPException(
            status_code=403,
            detail=(
                "Operator or administrator access is required."
            ),
        )

    return user