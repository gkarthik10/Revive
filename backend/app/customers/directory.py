"""
Revive - Customer Directory

Real-world problem this solves
-------------------------------
Every surface in Revive (live checkout, Promise-to-Pay, A2A
settlement, the synthetic case pipeline) needs to answer the same
question: "who is this customer, and how do we reach them?" — a
customer_id, a display name, and an email.

Before this module existed, that information had nowhere durable to
live. Each surface either had to ask a human to type a customer_id
by hand (which nobody actually knows — real operators don't have
customer IDs memorized, and two people leaving it blank would
silently collide into the same fake "cust_demo" identity), or the
information was captured once and then discarded, so the next
screen that needed it had nothing to fall back to.

The Customer Directory is the fix: a single durable, keyed-by-email
store that:

  1. Lets any surface resolve a customer from just an email address
     (the one thing an operator actually knows) instead of an
     invented ID — `resolve()` reuses the same customer_id for the
     same email every time, and generates a new, collision-free one
     the first time it sees an email.

  2. Lets any surface UPSERT whatever it just learned (a case gave
     us a name, a checkout gave us an email, a webhook gave us a
     phone number) so that information becomes available to every
     *other* surface that only has the customer_id.

This does not replace the authoritative case data in cases.json /
live_cases.json — it fills the gap those files were never designed
to cover (there is no email column in cases.json at all), and it is
the thing that makes "captured once, available everywhere" actually
true across live payments, Promise-to-Pay, and A2A settlement.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CUSTOMERS_FILE = Path(
    os.getenv(
        "CUSTOMERS_FILE",
        str(DATA_DIR / "customers.json"),
    )
)

_lock = threading.RLock()


def _email_key(email: str) -> str:
    return email.strip().lower()


def _derive_customer_id(email: str | None) -> str:
    """
    Deterministic customer_id derived from an email address, so the
    same real-world customer always resolves to the same ID even if
    two different surfaces independently derive it (no coordination
    needed, no database round trip required to stay consistent).

    Falls back to a short random ID only when there is truly no
    email to anchor on — that ID is *not* reused across requests
    the way an email-derived one is, so callers should prefer
    supplying an email whenever one is available.
    """

    if email:
        digest = hashlib.sha256(
            _email_key(email).encode("utf-8")
        ).hexdigest()
        return f"cust_{digest[:10]}"

    return f"cust_{uuid.uuid4().hex[:10]}"


class CustomerDirectory:
    """
    Thread-safe, append/update JSON-backed store mapping
    customer_id -> {customer_id, name, email, contact}.

    Keeps a secondary email -> customer_id index in memory (rebuilt
    from disk on load) so resolve() can find an existing customer
    by email without a linear scan on every call.
    """

    def __init__(self, path: Path = CUSTOMERS_FILE) -> None:
        self.path = Path(path)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._id_by_email: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        with _lock:
            self._by_id = {}
            self._id_by_email = {}

            if not self.path.exists():
                return

            try:
                with self.path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError):
                return

            if not isinstance(raw, list):
                return

            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                customer_id = entry.get("customer_id")
                if not customer_id:
                    continue
                self._by_id[customer_id] = entry
                email = entry.get("email")
                if email:
                    self._id_by_email[_email_key(email)] = customer_id

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(
                list(self._by_id.values()),
                f,
                indent=2,
                ensure_ascii=False,
            )

    # --------------------------------------------------------
    # Read
    # --------------------------------------------------------

    def get(self, customer_id: str | None) -> dict[str, Any] | None:
        if not customer_id:
            return None
        with _lock:
            entry = self._by_id.get(customer_id)
            return dict(entry) if entry else None

    def find_by_email(self, email: str | None) -> dict[str, Any] | None:
        if not email:
            return None
        with _lock:
            customer_id = self._id_by_email.get(_email_key(email))
            if not customer_id:
                return None
            entry = self._by_id.get(customer_id)
            return dict(entry) if entry else None

    def list_all(self) -> list[dict[str, Any]]:
        with _lock:
            return list(self._by_id.values())

    # --------------------------------------------------------
    # Resolve + upsert
    # --------------------------------------------------------

    def resolve(
        self,
        customer_id: str | None = None,
        name: str | None = None,
        email: str | None = None,
        contact: str | None = None,
    ) -> dict[str, Any]:
        """
        Resolve a customer_id for the given identity hints and
        persist/merge whatever new information was supplied.

        Precedence for the returned customer_id:
          1. An explicitly supplied customer_id (e.g. an existing
             case already has one) — always respected as-is.
          2. An existing directory entry matched by email — so a
             repeat customer keeps the same ID even if they never
             supply it directly.
          3. A newly derived ID (from email if present, otherwise
             random).

        Any newly supplied name/email/contact is merged onto the
        resolved entry without ever erasing a previously known
        value with a blank one.
        """

        with _lock:
            resolved_id = (
                customer_id
                or (
                    self._id_by_email.get(_email_key(email))
                    if email
                    else None
                )
                or _derive_customer_id(email)
            )

            existing = self._by_id.get(resolved_id, {})

            merged = {
                "customer_id": resolved_id,
                "name": name or existing.get("name"),
                "email": email or existing.get("email"),
                "contact": contact or existing.get("contact"),
            }

            self._by_id[resolved_id] = merged

            if merged["email"]:
                self._id_by_email[_email_key(merged["email"])] = resolved_id

            self._save()

            return dict(merged)


customer_directory = CustomerDirectory()
