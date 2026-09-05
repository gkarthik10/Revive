"""
Revive - Auth / User Store

Private team workspace user store.

Users are persisted in:
    app/data/users.json

Roles:
    admin    -> full administration
    operator -> recovery operations
    viewer   -> read-only access

Backward compatibility:
    Existing "agent" users are treated as "operator".

Bootstrap:
    If users.json is empty, the first account may be created through
    the bootstrap registration endpoint and becomes ADMIN.

After the first account exists, public registration is disabled.
Administrators create additional teammates through the protected
team-management endpoint.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.auth.security import hash_password, verify_password


USERS_FILE = Path(__file__).resolve().parent.parent / "data" / "users.json"

_lock = threading.RLock()

VALID_ROLES = {"admin", "operator", "viewer"}

ROLE_ALIASES = {
    "agent": "operator",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_role(role: str | None) -> str:
    """
    Normalize historical role names into the current Revive role model.
    """

    value = str(role or "").strip().lower()

    return ROLE_ALIASES.get(value, value)


def _read_all() -> list[dict[str, Any]]:
    if not USERS_FILE.exists():
        return []

    try:
        with USERS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []

    return data if isinstance(data, list) else []


def _write_all(users: list[dict[str, Any]]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with USERS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(
            users,
            fh,
            indent=2,
            ensure_ascii=False,
        )


def _public(user: dict[str, Any]) -> dict[str, Any]:
    """
    Strip password_hash before the record reaches an API response.

    Existing "agent" records are exposed as "operator" so the frontend
    sees one consistent role vocabulary.
    """

    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": normalize_role(user.get("role")),
        "created_at": user["created_at"],
    }


class UserExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InvalidRoleError(Exception):
    pass


class CannotRemoveLastAdminError(Exception):
    pass


def get_user_by_email(email: str) -> dict[str, Any] | None:
    email_norm = email.strip().lower()

    with _lock:
        for user in _read_all():
            if str(user.get("email", "")).lower() == email_norm:
                return user

    return None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    with _lock:
        for user in _read_all():
            if user.get("id") == user_id:
                return user

    return None


def has_users() -> bool:
    with _lock:
        return bool(_read_all())


def count_admins() -> int:
    with _lock:
        return sum(
            1
            for user in _read_all()
            if normalize_role(user.get("role")) == "admin"
        )


def create_user(
    name: str,
    email: str,
    password: str,
    role: str | None = None,
) -> dict[str, Any]:
    """
    Create a user.

    If role is omitted:
        first user -> admin
        subsequent users -> operator

    This function itself does not decide whether the caller is allowed
    to create the user. That is handled by the API layer.
    """

    name_clean = name.strip()
    email_norm = email.strip().lower()

    if not name_clean:
        raise ValueError("Name is required.")

    if not email_norm:
        raise ValueError("Email is required.")

    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    with _lock:
        users = _read_all()

        if any(
            str(user.get("email", "")).lower() == email_norm
            for user in users
        ):
            raise UserExistsError(
                f"An account already exists for {email_norm}."
            )

        if role is None:
            assigned_role = "admin" if len(users) == 0 else "operator"
        else:
            assigned_role = normalize_role(role)

        if assigned_role not in VALID_ROLES:
            raise InvalidRoleError(
                "Role must be admin, operator, or viewer."
            )

        user = {
            "id": str(uuid.uuid4()),
            "name": name_clean,
            "email": email_norm,
            "password_hash": hash_password(password),
            "role": assigned_role,
            "created_at": _now_iso(),
        }

        users.append(user)
        _write_all(users)

        return _public(user)


def create_team_member(
    name: str,
    email: str,
    password: str,
    role: str,
) -> dict[str, Any]:
    """
    Admin-only teammate creation is exposed through the API.

    This deliberately reuses the same durable user store.
    """

    assigned_role = normalize_role(role)

    if assigned_role == "admin":
        # Admin can create another admin, but the UI defaults to
        # operator to reduce accidental privilege escalation.
        pass

    return create_user(
        name=name,
        email=email,
        password=password,
        role=assigned_role,
    )


def authenticate(email: str, password: str) -> dict[str, Any]:
    user = get_user_by_email(email)

    if user is None:
        raise InvalidCredentialsError("Incorrect email or password.")

    if not verify_password(
        password,
        user.get("password_hash", ""),
    ):
        raise InvalidCredentialsError("Incorrect email or password.")

    return _public(user)


def change_password(
    user_id: str,
    current_password: str,
    new_password: str,
) -> dict[str, Any]:
    """
    Change a signed-in user's own password.

    The caller must supply their current password — this is a
    self-service action, not an admin reset, so it re-verifies the
    account owner rather than trusting the JWT alone.
    """

    if len(new_password) < 8:
        raise ValueError("New password must be at least 8 characters.")

    with _lock:
        users = _read_all()

        target = None

        for user in users:
            if user.get("id") == user_id:
                target = user
                break

        if target is None:
            raise InvalidCredentialsError("Account no longer exists.")

        if not verify_password(
            current_password,
            target.get("password_hash", ""),
        ):
            raise InvalidCredentialsError("Current password is incorrect.")

        target["password_hash"] = hash_password(new_password)

        _write_all(users)

        return _public(target)


def list_users() -> list[dict[str, Any]]:
    with _lock:
        return [
            _public(user)
            for user in _read_all()
        ]


def set_role(
    user_id: str,
    role: str,
) -> dict[str, Any] | None:
    """
    Change a user's role.

    Safety:
        The final remaining ADMIN cannot be demoted.
    """

    assigned_role = normalize_role(role)

    if assigned_role not in VALID_ROLES:
        raise InvalidRoleError(
            "Role must be admin, operator, or viewer."
        )

    with _lock:
        users = _read_all()

        target = None

        for user in users:
            if user.get("id") == user_id:
                target = user
                break

        if target is None:
            return None

        current_role = normalize_role(
            target.get("role")
        )

        if (
            current_role == "admin"
            and assigned_role != "admin"
        ):
            admin_count = sum(
                1
                for user in users
                if normalize_role(user.get("role")) == "admin"
            )

            if admin_count <= 1:
                raise CannotRemoveLastAdminError(
                    "The last Revive administrator cannot be demoted."
                )

        target["role"] = assigned_role

        _write_all(users)

        return _public(target)


def delete_user(
    user_id: str,
    requesting_user_id: str,
) -> bool:
    """
    Remove a teammate.

    The current user cannot delete themselves.
    The last admin cannot be deleted.
    """

    if user_id == requesting_user_id:
        raise ValueError(
            "You cannot remove your own account."
        )

    with _lock:
        users = _read_all()

        target = None

        for user in users:
            if user.get("id") == user_id:
                target = user
                break

        if target is None:
            return False

        if normalize_role(target.get("role")) == "admin":
            admin_count = sum(
                1
                for user in users
                if normalize_role(user.get("role")) == "admin"
            )

            if admin_count <= 1:
                raise CannotRemoveLastAdminError(
                    "The last Revive administrator cannot be removed."
                )

        users = [
            user
            for user in users
            if user.get("id") != user_id
        ]

        _write_all(users)

        return True