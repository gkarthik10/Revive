"""
Revive - Auth / Authorization Middleware

Authentication:
    Every /api/* route requires a valid JWT unless explicitly public.

Authorization:
    ADMIN    -> full access
    OPERATOR -> operational access
    VIEWER   -> read-only access

The middleware intentionally keeps Razorpay webhook access public because
Razorpay authenticates that route using its own webhook signature.

Bootstrap registration is public only because the route itself checks
whether users already exist.
"""

from __future__ import annotations

import jwt

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.security import decode_access_token
from app.auth.store import get_user_by_id, normalize_role


PUBLIC_PREFIXES = (
    "/api/auth/register",
    "/api/auth/login",
    "/api/health",
    "/api/payments/webhook",
    "/api/voice-audio/",
)


# ============================================================
# Routes where POST/PUT/PATCH/DELETE represent operational writes.
#
# Viewer users may still use GET endpoints and the Copilot chat.
# Copilot actions themselves are confirmation-gated by the Copilot
# service; the middleware therefore allows the chat/confirmation
# endpoint through for authenticated users, while the actual
# downstream operational permissions remain enforced by the
# application layer.
# ============================================================

VIEWER_BLOCKED_PREFIXES = (
    "/api/run-batch",
    "/api/payments/checkout",
    "/api/payments/live-cases/",
    "/api/payments/live-cases",
    "/api/a2a/live/",
    "/api/promises",
    "/api/cases/",
    "/api/simulate",
    "/api/auth/users",
)


def _json_error(
    status_code: int,
    detail: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):

    async def dispatch(
        self,
        request: Request,
        call_next,
    ):
        path = request.url.path
        method = request.method.upper()

        # ----------------------------------------------------
        # Non-API routes
        # ----------------------------------------------------

        if not path.startswith("/api/"):
            return await call_next(request)

        # ----------------------------------------------------
        # Explicit public routes
        # ----------------------------------------------------

        if any(
            path.startswith(prefix)
            for prefix in PUBLIC_PREFIXES
        ):
            return await call_next(request)

        # ----------------------------------------------------
        # CORS preflight
        # ----------------------------------------------------

        if method == "OPTIONS":
            return await call_next(request)

        # ----------------------------------------------------
        # Authentication
        # ----------------------------------------------------

        authorization = request.headers.get(
            "authorization"
        )

        if (
            not authorization
            or not authorization.startswith("Bearer ")
        ):
            return _json_error(
                401,
                "Missing or malformed Authorization header.",
            )

        token = authorization.removeprefix(
            "Bearer "
        ).strip()

        try:
            claims = decode_access_token(
                token
            )

        except jwt.ExpiredSignatureError:
            return _json_error(
                401,
                "Session expired. Please sign in again.",
            )

        except jwt.PyJWTError:
            return _json_error(
                401,
                "Invalid session token.",
            )

        # ----------------------------------------------------
        # Load the CURRENT user record.
        #
        # Do not trust role information from an old JWT when the
        # user may have been demoted by an administrator.
        # ----------------------------------------------------

        user_id = claims.get(
            "sub",
            "",
        )

        user = get_user_by_id(
            user_id
        )

        if user is None:
            return _json_error(
                401,
                "Account no longer exists.",
            )

        role = normalize_role(
            user.get("role")
        )

        if role not in {
            "admin",
            "operator",
            "viewer",
        }:
            return _json_error(
                403,
                "Your account has an invalid Revive role.",
            )

        # ----------------------------------------------------
        # Viewer authorization
        # ----------------------------------------------------

        if role == "viewer":
            blocked = any(
                path.startswith(prefix)
                for prefix in VIEWER_BLOCKED_PREFIXES
            )

            if blocked and method in {
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            }:
                return _json_error(
                    403,
                    (
                        "Viewer access is read-only. "
                        "Ask a Revive administrator or operator "
                        "to perform this action."
                    ),
                )

        # ----------------------------------------------------
        # Team administration
        # ----------------------------------------------------

        if path.startswith("/api/auth/users"):
            if role != "admin":
                return _json_error(
                    403,
                    "Administrator access is required for team management.",
                )

        return await call_next(request)