"""
Revive - Private Team Authentication API

Public:
    POST /api/auth/register
        Bootstrap only. Available only when users.json is empty.

    POST /api/auth/login
        Exchange email + password for JWT.

Protected:
    GET /api/auth/me

    POST /api/auth/change-password
        Self-service password change for the signed-in user.

    GET /api/auth/users
        Team roster.

    POST /api/auth/users
        ADMIN only. Create teammate directly.

    PATCH /api/auth/users/{id}/role
        ADMIN only.

    DELETE /api/auth/users/{id}
        ADMIN only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.auth.deps import get_current_user
from app.auth.security import create_access_token
from app.auth.store import (
    CannotRemoveLastAdminError,
    InvalidCredentialsError,
    InvalidRoleError,
    UserExistsError,
    authenticate,
    change_password,
    create_team_member,
    create_user,
    has_users,
    list_users,
    set_role,
    delete_user,
)


router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
)


class RegisterRequest(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=120,
    )
    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=200,
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(
        min_length=1,
        max_length=200,
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(
        min_length=1,
        max_length=200,
    )
    new_password: str = Field(
        min_length=8,
        max_length=200,
    )


class TeamMemberCreateRequest(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=120,
    )
    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=200,
    )
    role: str = Field(
        default="operator",
        pattern="^(admin|operator|viewer)$",
    )


class RoleUpdateRequest(BaseModel):
    role: str = Field(
        pattern="^(admin|operator|viewer)$",
    )


def _token_response(user: dict) -> dict:
    token = create_access_token(
        {
            "sub": user["id"],
            "email": user["email"],
            "role": user["role"],
        }
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user,
    }


def _require_admin(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Administrator access is required.",
        )


# ============================================================
# Bootstrap registration
# ============================================================

@router.post("/register")
def register(body: RegisterRequest):
    """
    Bootstrap-only registration.

    The first account on a fresh installation becomes ADMIN.

    Once any account exists, public registration is permanently
    closed through this route. Additional teammates must be created
    by an administrator.
    """

    if has_users():
        raise HTTPException(
            status_code=403,
            detail=(
                "Public registration is disabled. "
                "Ask a Revive administrator to create your team account."
            ),
        )

    try:
        user = create_user(
            body.name,
            body.email,
            body.password,
            role="admin",
        )

    except UserExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    return _token_response(user)


# ============================================================
# Login
# ============================================================

@router.post("/login")
def login(body: LoginRequest):
    try:
        user = authenticate(
            body.email,
            body.password,
        )

    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=401,
            detail=str(exc),
        )

    return _token_response(user)


# ============================================================
# Current user
# ============================================================

@router.get("/me")
def me(
    user: dict = Depends(get_current_user),
):
    return user


# ============================================================
# Self-service password change
# ============================================================

@router.post("/change-password")
def change_own_password(
    body: ChangePasswordRequest,
    user: dict = Depends(get_current_user),
):
    """
    Lets a signed-in user change their own password.

    Requires the current password, independent of the JWT, so a
    hijacked session token alone isn't enough to lock the real owner
    out of their account.
    """

    try:
        updated = change_password(
            user_id=user["id"],
            current_password=body.current_password,
            new_password=body.new_password,
        )

    except InvalidCredentialsError as exc:
        # Intentionally 400, not 401: this means "the current_password
        # field you typed is wrong", not "your session token is invalid".
        # The frontend's global axios interceptor force-logs-out on any
        # 401, so using 401 here would kick a signed-in user back to the
        # login screen instead of showing them the inline form error.
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    return {
        "success": True,
        "user": updated,
    }


# ============================================================
# Team roster
# ============================================================

@router.get("/users")
def users(
    user: dict = Depends(get_current_user),
):
    """
    Signed-in teammates can see the team roster.
    """

    return {
        "success": True,
        "users": list_users(),
    }


# ============================================================
# Admin creates teammate
# ============================================================

@router.post("/users")
def create_user_for_team(
    body: TeamMemberCreateRequest,
    user: dict = Depends(get_current_user),
):
    """
    Create a team member directly.

    ADMIN only.
    """

    _require_admin(user)

    try:
        created = create_team_member(
            name=body.name,
            email=body.email,
            password=body.password,
            role=body.role,
        )

    except UserExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    except InvalidRoleError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    return {
        "success": True,
        "user": created,
    }


# ============================================================
# Admin role management
# ============================================================

@router.patch("/users/{user_id}/role")
def update_role(
    user_id: str,
    body: RoleUpdateRequest,
    user: dict = Depends(get_current_user),
):
    _require_admin(user)

    try:
        updated = set_role(
            user_id,
            body.role,
        )

    except CannotRemoveLastAdminError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    except InvalidRoleError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="No such user.",
        )

    return {
        "success": True,
        "user": updated,
    }


# ============================================================
# Admin removes teammate
# ============================================================

@router.delete("/users/{user_id}")
def remove_user(
    user_id: str,
    user: dict = Depends(get_current_user),
):
    _require_admin(user)

    try:
        deleted = delete_user(
            user_id=user_id,
            requesting_user_id=user["id"],
        )

    except CannotRemoveLastAdminError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="No such user.",
        )

    return {
        "success": True,
        "message": "Team member removed.",
    }