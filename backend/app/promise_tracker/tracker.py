"""
Revive - Module 5
Promise-to-Pay Tracker

Tracks customer payment promises as a deterministic state machine.

State flow:

    NONE
      |
      v
   PROMISED
    /     \
   v       v
 PAID    BROKEN
   |       |
   v       v
 CLOSED  RE-ESCALATE

A promise-to-pay has an important side effect:

    promise_to_pay_active = True

This automatically activates the hard-stop already implemented
by Module 3's PolicyEngine.

Production improvements in this version:

    1. Durable JSON persistence.
    2. Thread-safe state changes.
    3. Unique promise_id for every promise.
    4. Customer ID and customer name metadata.
    5. Invoice ID metadata.
    6. Original/outstanding amount metadata.
    7. Payment reference/source/evidence fields.
    8. Append-only promise history.
    9. Durable transition/audit history.
   10. Restart recovery.
   11. Stronger validation.
   12. Backward compatibility with existing case_id-based API.
   13. Existing PolicyEngine hard-stop behavior preserved.

The tracker is deliberately deterministic and explainable.
No LLM is used.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.policy import (
    CaseState,
    PolicyEngine,
)


# ============================================================
# Persistence
# ============================================================

DEFAULT_DATA_DIR = (
    Path(__file__).resolve().parent.parent / "data"
)

DEFAULT_STORAGE_FILE = (
    DEFAULT_DATA_DIR / "promise_tracker.json"
)


# ============================================================
# Promise status
# ============================================================

class PromiseStatus(str, Enum):
    """
    Lifecycle state of a payment promise.
    """

    NONE = "none"

    PROMISED = "promised"

    PAID = "paid"

    BROKEN = "broken"

    CLOSED = "closed"


# ============================================================
# Promise record
# ============================================================

@dataclass(frozen=True)
class PromiseRecord:
    """
    Immutable record describing a promise-to-pay.

    The first six fields are intentionally preserved from the
    original Module 5 implementation for compatibility.

    Additional metadata fields make the promise independently
    identifiable and suitable for production audit/reconciliation.
    """

    # Original fields
    case_id: str

    promised_amount: float

    promise_date: datetime

    created_at: datetime

    status: PromiseStatus

    reason: str

    # Production identity
    promise_id: str = ""

    # Customer/invoice identity
    customer_id: str | None = None

    customer_name: str | None = None

    customer_email: str | None = None

    invoice_id: str | None = None

    # Financial context
    original_amount: float | None = None

    outstanding_amount: float | None = None

    # Payment evidence
    payment_reference: str | None = None

    payment_source: str | None = None

    payment_verified: bool = False

    # Last modification
    updated_at: datetime | None = None

    # Razorpay Payment Link lifecycle
    payment_link_id: str | None = None
    payment_link_url: str | None = None
    payment_link_expire_by: datetime | None = None


# ============================================================
# State transition record
# ============================================================

@dataclass(frozen=True)
class PromiseTransition:
    """
    Records a state transition.

    Every transition is auditable and persisted.
    """

    # Original fields
    case_id: str

    previous_status: PromiseStatus

    new_status: PromiseStatus

    timestamp: datetime

    reason: str

    # Production identity
    promise_id: str = ""


# ============================================================
# Promise Tracker
# ============================================================

class PromiseTracker:
    """
    Manages promise-to-pay records and associated CaseState.

    Each case has one current promise record.

    Historical promise records are retained separately so that
    replacing a BROKEN promise with a new promise does not destroy
    the previous promise's history.

    The tracker does not directly send messages or collect money.
    It manages promise lifecycle, policy state, and audit data.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        storage_path: Path | str | None = None,
        persistence_enabled: bool = True,
    ) -> None:

        self.policy_engine = (
            policy_engine
            if policy_engine is not None
            else PolicyEngine()
        )

        self.persistence_enabled = persistence_enabled

        self.storage_path = (
            Path(storage_path)
            if storage_path is not None
            else DEFAULT_STORAGE_FILE
        )

        self._lock = threading.RLock()

        # ----------------------------------------------------
        # Current promise per case.
        #
        # Kept as dict[str, PromiseRecord] for compatibility
        # with the existing API/frontend.
        # ----------------------------------------------------

        self.records: dict[
            str,
            PromiseRecord,
        ] = {}

        # ----------------------------------------------------
        # Case policy state.
        # ----------------------------------------------------

        self.states: dict[
            str,
            CaseState,
        ] = {}

        # ----------------------------------------------------
        # Full audit trail.
        # ----------------------------------------------------

        self.transitions: list[
            PromiseTransition
        ] = []

        # ----------------------------------------------------
        # Historical promise snapshots.
        #
        # This prevents an old BROKEN promise from disappearing
        # when a new promise is created for the same case.
        # ----------------------------------------------------

        self.promise_history: list[
            PromiseRecord
        ] = []

        if self.persistence_enabled:
            self._load()

    # ========================================================
    # Serialization helpers
    # ========================================================

    @staticmethod
    def _datetime_to_json(
        value: datetime | None,
    ) -> str | None:

        if value is None:
            return None

        return value.isoformat()

    @staticmethod
    def _datetime_from_json(
        value: str | None,
    ) -> datetime | None:

        if not value:
            return None

        return datetime.fromisoformat(value)

    @staticmethod
    def _status_to_value(
        status: PromiseStatus,
    ) -> str:

        if isinstance(status, PromiseStatus):
            return status.value

        return str(status)

    @classmethod
    def _record_to_dict(
        cls,
        record: PromiseRecord,
    ) -> dict[str, Any]:

        return {
            "case_id": record.case_id,
            "promised_amount": record.promised_amount,
            "promise_date": cls._datetime_to_json(
                record.promise_date
            ),
            "created_at": cls._datetime_to_json(
                record.created_at
            ),
            "status": cls._status_to_value(
                record.status
            ),
            "reason": record.reason,
            "promise_id": record.promise_id,
            "customer_id": record.customer_id,
            "customer_name": record.customer_name,
            "customer_email": record.customer_email,
            "invoice_id": record.invoice_id,
            "original_amount": record.original_amount,
            "outstanding_amount": record.outstanding_amount,
            "payment_reference": record.payment_reference,
            "payment_source": record.payment_source,
            "payment_verified": record.payment_verified,
            "updated_at": cls._datetime_to_json(
                record.updated_at
            ),
            "payment_link_id": record.payment_link_id,
            "payment_link_url": record.payment_link_url,
            "payment_link_expire_by": cls._datetime_to_json(
                record.payment_link_expire_by
            ),
        }

    @classmethod
    def _record_from_dict(
        cls,
        data: dict[str, Any],
    ) -> PromiseRecord:

        return PromiseRecord(
            case_id=str(
                data["case_id"]
            ),
            promised_amount=float(
                data["promised_amount"]
            ),
            promise_date=(
                cls._datetime_from_json(
                    data["promise_date"]
                )
                or datetime.now()
            ),
            created_at=(
                cls._datetime_from_json(
                    data["created_at"]
                )
                or datetime.now()
            ),
            status=PromiseStatus(
                data.get(
                    "status",
                    PromiseStatus.NONE.value,
                )
            ),
            reason=str(
                data.get(
                    "reason",
                    "",
                )
            ),
            promise_id=str(
                data.get(
                    "promise_id",
                    "",
                )
            ),
            customer_id=data.get(
                "customer_id"
            ),
            customer_name=data.get(
                "customer_name"
            ),
            customer_email=data.get("customer_email"),
            invoice_id=data.get(
                "invoice_id"
            ),
            original_amount=(
                float(
                    data["original_amount"]
                )
                if data.get(
                    "original_amount"
                ) is not None
                else None
            ),
            outstanding_amount=(
                float(
                    data["outstanding_amount"]
                )
                if data.get(
                    "outstanding_amount"
                ) is not None
                else None
            ),
            payment_reference=data.get(
                "payment_reference"
            ),
            payment_source=data.get(
                "payment_source"
            ),
            payment_verified=bool(
                data.get(
                    "payment_verified",
                    False,
                )
            ),
            updated_at=(
                cls._datetime_from_json(
                    data.get(
                        "updated_at"
                    )
                )
            ),
            payment_link_id=data.get("payment_link_id"),
            payment_link_url=data.get("payment_link_url"),
            payment_link_expire_by=(
                cls._datetime_from_json(
                    data.get("payment_link_expire_by")
                )
            ),
        )

    @classmethod
    def _transition_to_dict(
        cls,
        transition: PromiseTransition,
    ) -> dict[str, Any]:

        return {
            "case_id": transition.case_id,
            "previous_status": cls._status_to_value(
                transition.previous_status
            ),
            "new_status": cls._status_to_value(
                transition.new_status
            ),
            "timestamp": cls._datetime_to_json(
                transition.timestamp
            ),
            "reason": transition.reason,
            "promise_id": transition.promise_id,
        }

    @classmethod
    def _transition_from_dict(
        cls,
        data: dict[str, Any],
    ) -> PromiseTransition:

        return PromiseTransition(
            case_id=str(
                data["case_id"]
            ),
            previous_status=PromiseStatus(
                data.get(
                    "previous_status",
                    PromiseStatus.NONE.value,
                )
            ),
            new_status=PromiseStatus(
                data.get(
                    "new_status",
                    PromiseStatus.NONE.value,
                )
            ),
            timestamp=(
                cls._datetime_from_json(
                    data.get(
                        "timestamp"
                    )
                )
                or datetime.now()
            ),
            reason=str(
                data.get(
                    "reason",
                    "",
                )
            ),
            promise_id=str(
                data.get(
                    "promise_id",
                    "",
                )
            ),
        )

    # ========================================================
    # Persistence
    # ========================================================

    def _ensure_storage_directory(self) -> None:

        self.storage_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    def _save(self) -> None:
        """
        Persist tracker state.

        Direct write is intentionally used instead of rename-based
        atomic replacement because the Revive project can run with
        Windows/Docker bind-mounted filesystems where rename/replace
        can fail with 'Device or resource busy'.
        """

        if not self.persistence_enabled:
            return

        payload = {
            "version": 2,
            "records": {
                case_id: self._record_to_dict(
                    record
                )
                for case_id, record
                in self.records.items()
            },
            "promise_history": [
                self._record_to_dict(
                    record
                )
                for record
                in self.promise_history
            ],
            "transitions": [
                self._transition_to_dict(
                    transition
                )
                for transition
                in self.transitions
            ],
        }

        self._ensure_storage_directory()

        self.storage_path.write_text(
            json.dumps(
                payload,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _load(self) -> None:
        """
        Load persisted tracker state.

        Missing files are normal on first startup.

        A malformed persistence file is not allowed to crash
        application startup. The tracker starts empty and reports
        the problem to stdout so the issue is visible.
        """

        if not self.persistence_enabled:
            return

        if not self.storage_path.exists():
            return

        try:

            raw = self.storage_path.read_text(
                encoding="utf-8"
            ).strip()

            if not raw:
                return

            payload = json.loads(raw)

            records_data = payload.get(
                "records",
                {},
            )

            history_data = payload.get(
                "promise_history",
                [],
            )

            transitions_data = payload.get(
                "transitions",
                [],
            )

            loaded_records: dict[
                str,
                PromiseRecord,
            ] = {}

            for case_id, data in records_data.items():

                record = self._record_from_dict(
                    data
                )

                loaded_records[
                    str(case_id)
                ] = record

            loaded_history: list[
                PromiseRecord
            ] = []

            for data in history_data:

                loaded_history.append(
                    self._record_from_dict(
                        data
                    )
                )

            loaded_transitions: list[
                PromiseTransition
            ] = []

            for data in transitions_data:

                loaded_transitions.append(
                    self._transition_from_dict(
                        data
                    )
                )

            self.records = loaded_records

            self.promise_history = (
                loaded_history
            )

            self.transitions = (
                loaded_transitions
            )

            # ------------------------------------------------
            # Reconstruct policy state from current promises.
            # ------------------------------------------------

            for case_id, record in self.records.items():

                state = CaseState(
                    case_id=case_id
                )

                if record.status == PromiseStatus.PROMISED:

                    state.promise_to_pay_active = True

                    state.promise_date = (
                        record.promise_date
                    )

                else:

                    state.promise_to_pay_active = False

                self.states[
                    case_id
                ] = state

        except Exception as error:

            print(
                "[PromiseTracker] WARNING: "
                f"Could not load persistence file "
                f"{self.storage_path}: {error}"
            )

            self.records = {}

            self.promise_history = []

            self.transitions = []

            self.states = {}

    # ========================================================
    # Utility helpers
    # ========================================================

    @staticmethod
    def _generate_promise_id() -> str:

        return (
            "PTP-"
            + uuid4().hex
        )

    @staticmethod
    def _validate_case_id(
        case_id: str,
    ) -> None:

        if not isinstance(
            case_id,
            str,
        ):

            raise ValueError(
                "Case ID must be a string."
            )

        if not case_id.strip():

            raise ValueError(
                "Case ID cannot be empty."
            )

    @staticmethod
    def _validate_amount(
        amount: float,
        field_name: str,
    ) -> None:

        try:
            numeric_amount = float(
                amount
            )
        except (
            TypeError,
            ValueError,
        ):

            raise ValueError(
                f"{field_name} must be a valid number."
            )

        if numeric_amount <= 0:

            raise ValueError(
                f"{field_name} must be greater than zero."
            )

    @staticmethod
    def _append_history_if_new(
        history: list[PromiseRecord],
        record: PromiseRecord,
    ) -> None:

        if not record.promise_id:

            return

        for existing in history:

            if (
                existing.promise_id
                == record.promise_id
                and existing.status
                == record.status
            ):

                return

        history.append(record)

    # ========================================================
    # Get/create case state
    # ========================================================

    def get_state(
        self,
        case_id: str,
    ) -> CaseState:

        with self._lock:

            if case_id not in self.states:

                self.states[case_id] = CaseState(
                    case_id=case_id
                )

            return self.states[case_id]

    # ========================================================
    # Get current promise status
    # ========================================================

    def get_status(
        self,
        case_id: str,
    ) -> PromiseStatus:

        with self._lock:

            record = self.records.get(
                case_id
            )

            if record is None:

                return PromiseStatus.NONE

            return record.status

    # ========================================================
    # Get current promise
    # ========================================================

    def get_promise(
        self,
        case_id: str,
    ) -> PromiseRecord | None:

        with self._lock:

            return self.records.get(
                case_id
            )

    # ========================================================
    # Get promise by unique ID
    # ========================================================

    def get_promise_by_id(
        self,
        promise_id: str,
    ) -> PromiseRecord | None:

        with self._lock:

            for record in self.records.values():

                if record.promise_id == promise_id:

                    return record

            for record in reversed(
                self.promise_history
            ):

                if record.promise_id == promise_id:

                    return record

            return None

    # ========================================================
    # Get all current promises
    # ========================================================

    def get_all_promises(
        self,
    ) -> list[PromiseRecord]:

        with self._lock:

            return list(
                self.records.values()
            )

    # ========================================================
    # Get historical promises
    # ========================================================

    def get_promise_history(
        self,
        case_id: str | None = None,
    ) -> list[PromiseRecord]:

        with self._lock:

            if case_id is None:

                return list(
                    self.promise_history
                )

            return [
                record
                for record
                in self.promise_history
                if record.case_id
                == case_id
            ]

    # ========================================================
    # Create promise
    # ========================================================

    def create_promise(
        self,
        case_id: str,
        promised_amount: float,
        promise_date: datetime,
        created_at: datetime | None = None,
        customer_id: str | None = None,
        customer_name: str | None = None,
        customer_email: str | None = None,
        invoice_id: str | None = None,
        original_amount: float | None = None,
        outstanding_amount: float | None = None,
    ) -> PromiseRecord:

        with self._lock:

            self._validate_case_id(
                case_id
            )

            self._validate_amount(
                promised_amount,
                "Promised amount",
            )

            if created_at is None:

                created_at = datetime.now()

            if not isinstance(
                promise_date,
                datetime,
            ):

                raise ValueError(
                    "Promise date must be a datetime."
                )

            if promise_date <= created_at:

                raise ValueError(
                    "Promise date must be in the future."
                )

            if original_amount is not None:

                self._validate_amount(
                    original_amount,
                    "Original amount",
                )

                if (
                    float(promised_amount)
                    > float(original_amount)
                ):

                    raise ValueError(
                        "Promised amount cannot exceed "
                        "the original amount."
                    )

            if outstanding_amount is not None:

                self._validate_amount(
                    outstanding_amount,
                    "Outstanding amount",
                )

                if (
                    float(promised_amount)
                    > float(outstanding_amount)
                ):

                    raise ValueError(
                        "Promised amount cannot exceed "
                        "the outstanding amount."
                    )

            current_status = self.get_status(
                case_id
            )

            # ------------------------------------------------
            # Prevent invalid duplicate promises
            # ------------------------------------------------

            if current_status == PromiseStatus.PROMISED:

                raise ValueError(
                    "An active promise already exists "
                    f"for case {case_id}."
                )

            if current_status == PromiseStatus.PAID:

                raise ValueError(
                    "A paid promise cannot be replaced."
                )

            # ------------------------------------------------
            # Closed cases cannot create new promises
            # ------------------------------------------------

            if current_status == PromiseStatus.CLOSED:

                raise ValueError(
                    "A closed case cannot create a new promise."
                )

            previous_status = current_status

            # ------------------------------------------------
            # Preserve previous promise in history.
            #
            # This matters when a BROKEN promise is followed
            # by a new promise for the same case.
            # ------------------------------------------------

            previous_record = self.records.get(
                case_id
            )

            if previous_record is not None:

                self._append_history_if_new(
                    self.promise_history,
                    previous_record,
                )

            # ------------------------------------------------
            # Create unique promise
            # ------------------------------------------------

            promise_id = (
                self._generate_promise_id()
            )

            record = PromiseRecord(
                case_id=case_id,
                promised_amount=float(
                    promised_amount
                ),
                promise_date=promise_date,
                created_at=created_at,
                status=PromiseStatus.PROMISED,
                reason=(
                    "Customer committed to payment by the "
                    "specified promise date."
                ),
                promise_id=promise_id,
                customer_id=customer_id,
                customer_name=customer_name,
                customer_email=customer_email,
                invoice_id=invoice_id,
                original_amount=(
                    float(original_amount)
                    if original_amount is not None
                    else None
                ),
                outstanding_amount=(
                    float(outstanding_amount)
                    if outstanding_amount is not None
                    else None
                ),
                payment_reference=None,
                payment_source=None,
                payment_verified=False,
                updated_at=created_at,
                payment_link_id=None,
                payment_link_url=None,
                payment_link_expire_by=None,
            )

            self.records[
                case_id
            ] = record

            # ------------------------------------------------
            # Activate policy hard stop
            # ------------------------------------------------

            state = self.get_state(
                case_id
            )

            state.promise_to_pay_active = True

            state.promise_date = promise_date

            # ------------------------------------------------
            # Audit transition
            # ------------------------------------------------

            self.transitions.append(
                PromiseTransition(
                    case_id=case_id,
                    previous_status=previous_status,
                    new_status=PromiseStatus.PROMISED,
                    timestamp=created_at,
                    reason=(
                        "Promise created; automated contact "
                        "must be blocked until promise resolution."
                    ),
                    promise_id=promise_id,
                )
            )

            self._save()

            return record

    # ========================================================
    # Attach Razorpay Payment Link
    # ========================================================

    def attach_payment_link(
        self,
        case_id: str,
        payment_link_id: str,
        payment_link_url: str,
        payment_link_expire_by: datetime | None = None,
    ) -> PromiseRecord:
        """Persist the Razorpay Payment Link for the active promise."""

        with self._lock:
            current = self.records.get(case_id)
            if current is None:
                raise ValueError(f"No promise exists for case {case_id}.")
            if current.status != PromiseStatus.PROMISED:
                raise ValueError(
                    f"Cannot attach a payment link to status {current.status.value}."
                )

            updated_at = datetime.now()
            updated = PromiseRecord(
                case_id=current.case_id,
                promised_amount=current.promised_amount,
                promise_date=current.promise_date,
                created_at=current.created_at,
                status=current.status,
                reason=current.reason,
                promise_id=current.promise_id,
                customer_id=current.customer_id,
                customer_name=current.customer_name,
                customer_email=current.customer_email,
                invoice_id=current.invoice_id,
                original_amount=current.original_amount,
                outstanding_amount=current.outstanding_amount,
                payment_reference=current.payment_reference,
                payment_source=current.payment_source,
                payment_verified=current.payment_verified,
                updated_at=updated_at,
                payment_link_id=payment_link_id,
                payment_link_url=payment_link_url,
                payment_link_expire_by=payment_link_expire_by,
            )
            self.records[case_id] = updated
            self._save()
            return updated

    # ========================================================
    # Mark paid
    # ========================================================

    def mark_paid(
        self,
        case_id: str,
        paid_at: datetime | None = None,
        payment_reference: str | None = None,
        payment_source: str | None = "manual",
        payment_verified: bool = False,
    ) -> PromiseRecord:

        with self._lock:

            if paid_at is None:

                paid_at = datetime.now()

            current = self.records.get(
                case_id
            )

            if current is None:

                raise ValueError(
                    f"No promise exists for case {case_id}."
                )

            if current.status != PromiseStatus.PROMISED:

                raise ValueError(
                    f"Cannot mark promise as paid from "
                    f"status {current.status.value}."
                )

            # ------------------------------------------------
            # Preserve previous state in history.
            # ------------------------------------------------

            self._append_history_if_new(
                self.promise_history,
                current,
            )

            # ------------------------------------------------
            # Update record.
            #
            # payment_verified explicitly distinguishes
            # lifecycle completion from authoritative payment
            # evidence.
            # ------------------------------------------------

            updated = PromiseRecord(
                case_id=current.case_id,
                promised_amount=current.promised_amount,
                promise_date=current.promise_date,
                created_at=current.created_at,
                status=PromiseStatus.PAID,
                reason=(
                    "Payment received against the "
                    "promise-to-pay."
                ),
                promise_id=current.promise_id,
                customer_id=current.customer_id,
                customer_name=current.customer_name,
                customer_email=current.customer_email,
                invoice_id=current.invoice_id,
                original_amount=current.original_amount,
                outstanding_amount=current.outstanding_amount,
                payment_reference=payment_reference,
                payment_source=payment_source,
                payment_verified=bool(
                    payment_verified
                ),
                updated_at=paid_at,
                payment_link_id=current.payment_link_id,
                payment_link_url=current.payment_link_url,
                payment_link_expire_by=current.payment_link_expire_by,
            )

            self.records[
                case_id
            ] = updated

            # ------------------------------------------------
            # Remove active hard stop
            # ------------------------------------------------

            state = self.get_state(
                case_id
            )

            state.promise_to_pay_active = False

            # ------------------------------------------------
            # Transition
            # ------------------------------------------------

            evidence_text = (
                "authoritative payment evidence recorded"
                if payment_verified
                else "payment fulfillment marked without "
                     "authoritative payment verification"
            )

            self.transitions.append(
                PromiseTransition(
                    case_id=case_id,
                    previous_status=PromiseStatus.PROMISED,
                    new_status=PromiseStatus.PAID,
                    timestamp=paid_at,
                    reason=(
                        "Promise fulfilled; "
                        f"{evidence_text}."
                    ),
                    promise_id=current.promise_id,
                )
            )

            self._save()

            return updated

        # ========================================================
    # Mark broken
    # ========================================================

    def mark_broken(
        self,
        case_id: str,
        broken_at: datetime | None = None,
        reason: str = (
            "Promise deadline passed without verified payment."
        ),
    ) -> PromiseRecord:

        with self._lock:

            if broken_at is None:
                broken_at = datetime.now()

            current = self.records.get(case_id)

            if current is None:
                raise ValueError(
                    f"No promise exists for case {case_id}."
                )

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Only an ACTIVE promise can become BROKEN.
            #
            # PAID promises can never be converted to BROKEN,
            # even if a late/background check runs.
            # ------------------------------------------------

            if current.status != PromiseStatus.PROMISED:
                raise ValueError(
                    f"Cannot mark promise as broken from "
                    f"status {current.status.value}."
                )

            # ------------------------------------------------
            # Preserve previous state in history.
            # ------------------------------------------------

            self._append_history_if_new(
                self.promise_history,
                current,
            )

            # ------------------------------------------------
            # Create updated immutable record.
            # ------------------------------------------------

            updated = PromiseRecord(
                case_id=current.case_id,
                promised_amount=current.promised_amount,
                promise_date=current.promise_date,
                created_at=current.created_at,
                status=PromiseStatus.BROKEN,
                reason=reason,
                promise_id=current.promise_id,
                customer_id=current.customer_id,
                customer_name=current.customer_name,
                customer_email=current.customer_email,
                invoice_id=current.invoice_id,
                original_amount=current.original_amount,
                outstanding_amount=current.outstanding_amount,
                payment_reference=current.payment_reference,
                payment_source=current.payment_source,
                payment_verified=current.payment_verified,
                updated_at=broken_at,
                payment_link_id=current.payment_link_id,
                payment_link_url=current.payment_link_url,
                payment_link_expire_by=current.payment_link_expire_by,
            )

            self.records[case_id] = updated

            # ------------------------------------------------
            # Remove the Promise-to-Pay policy hard-stop.
            #
            # The commitment is no longer active after the
            # deadline has passed.
            # ------------------------------------------------

            state = self.get_state(case_id)

            state.promise_to_pay_active = False

            # ------------------------------------------------
            # Audit transition.
            # ------------------------------------------------

            self.transitions.append(
                PromiseTransition(
                    case_id=case_id,
                    previous_status=PromiseStatus.PROMISED,
                    new_status=PromiseStatus.BROKEN,
                    timestamp=broken_at,
                    reason=reason,
                    promise_id=current.promise_id,
                )
            )

            self._save()

            return updated

    # ========================================================
    # Automatically evaluate expired promises
    # ========================================================

    def evaluate_due_promises(
        self,
        now: datetime,
    ) -> list[PromiseRecord]:
        """
        Automatically expire Promise-to-Pay commitments whose
        exact promised date and time has passed without payment.

        `now` must be supplied in the same wall-clock timezone used
        when PromiseRecord.promise_date was created.

        Only PROMISED records are evaluated.

        PAID, BROKEN and CLOSED records are never modified.

        Returns only promises that changed from PROMISED -> BROKEN
        during this evaluation.
        """

        broken_promises: list[PromiseRecord] = []

        with self._lock:

            for case_id, record in list(
                self.records.items()
            ):

                # ------------------------------------------------
                # Ignore every resolved/non-active promise.
                # ------------------------------------------------

                if record.status != PromiseStatus.PROMISED:
                    continue

                # ------------------------------------------------
                # Exact deadline comparison:
                #
                #     promise_date <= current_time
                #
                # This means the promise remains active until the
                # promised date AND time has actually passed.
                # ------------------------------------------------

                if record.promise_date > now:
                    continue

                # ------------------------------------------------
                # Deadline passed and payment was not recorded.
                # ------------------------------------------------

                broken = self.mark_broken(
                    case_id=case_id,
                    broken_at=now,
                    reason=(
                        "Promise deadline passed without "
                        "verified payment."
                    ),
                )

                broken_promises.append(broken)

        return broken_promises

    # ========================================================
    # Promise metrics
    # ========================================================

    def metrics(self) -> dict[str, Any]:

        with self._lock:

            # ------------------------------------------------
            # Current case-level metrics.
            #
            # Kept compatible with the previous implementation.
            # ------------------------------------------------

            total_promises = len(
                self.records
            )

            promised = sum(
                1
                for record
                in self.records.values()
                if record.status
                == PromiseStatus.PROMISED
            )

            paid = sum(
                1
                for record
                in self.records.values()
                if record.status
                == PromiseStatus.PAID
            )

            broken = sum(
                1
                for record
                in self.records.values()
                if record.status
                == PromiseStatus.BROKEN
            )

            resolved = paid + broken

            kept_rate = (
                paid / resolved
                if resolved > 0
                else 0.0
            )

            # ------------------------------------------------
            # Historical count is useful for operational
            # reporting without changing the existing fields.
            # ------------------------------------------------

            historical_promises = len(
                self.promise_history
            )

            return {
                "total_promises": total_promises,
                "active_promises": promised,
                "promises_kept": paid,
                "promises_broken": broken,
                "promise_kept_rate": kept_rate,
                "historical_promises": historical_promises,
                "audit_transitions": len(
                    self.transitions
                ),
            }

    # ========================================================
    # Audit trail
    # ========================================================

    def get_audit_trail(
        self,
        case_id: str,
    ) -> list[PromiseTransition]:

        with self._lock:

            return [
                transition
                for transition
                in self.transitions
                if transition.case_id
                == case_id
            ]

    # ========================================================
    # Persistence status
    # ========================================================

    def persistence_info(
        self,
    ) -> dict[str, Any]:

        with self._lock:

            return {
                "enabled": self.persistence_enabled,
                "storage_path": str(
                    self.storage_path
                ),
                "exists": (
                    self.storage_path.exists()
                    if self.persistence_enabled
                    else False
                ),
                "current_records": len(
                    self.records
                ),
                "historical_records": len(
                    self.promise_history
                ),
                "transitions": len(
                    self.transitions
                ),
            }


# ============================================================
# Self-test
# ============================================================

def main() -> None:

    print("=" * 72)
    print("REVIVE — MODULE 5")
    print("Promise-to-Pay Tracker")
    print("=" * 72)

    # --------------------------------------------------------
    # Self-test must not modify production persistence.
    # --------------------------------------------------------

    tracker = PromiseTracker(
        persistence_enabled=False
    )

    # Fixed test time so results are deterministic.
    base_time = datetime(
        2026,
        8,
        29,
        14,
        30,
    )

    # ========================================================
    # TEST 1 — Create promise
    # ========================================================

    promise = tracker.create_promise(
        case_id="RV-PTP-001",
        promised_amount=50000,
        promise_date=datetime(
            2026,
            9,
            2,
            12,
            0,
        ),
        created_at=base_time,
        customer_id="cust-001",
        customer_name="Test Customer",
        invoice_id="INV-PTP-001",
        original_amount=50000,
        outstanding_amount=50000,
    )

    print()
    print("TEST 1 — Create promise")

    print(
        f"  Case:       {promise.case_id}"
    )

    print(
        f"  Promise ID: {promise.promise_id}"
    )

    print(
        f"  Customer:   {promise.customer_name}"
    )

    print(
        f"  Invoice:    {promise.invoice_id}"
    )

    print(
        f"  Amount:     ₹{promise.promised_amount:,.2f}"
    )

    print(
        f"  Status:     {promise.status.value}"
    )

    print(
        f"  Promise:    "
        f"{promise.promise_date.isoformat()}"
    )

    state = tracker.get_state(
        "RV-PTP-001"
    )

    print(
        f"  Active PTP: "
        f"{state.promise_to_pay_active}"
    )

    assert (
        promise.status
        == PromiseStatus.PROMISED
    )

    assert (
        promise.promise_id.startswith(
            "PTP-"
        )
    )

    assert (
        promise.customer_id
        == "cust-001"
    )

    assert (
        promise.invoice_id
        == "INV-PTP-001"
    )

    assert (
        state.promise_to_pay_active
        is True
    )

    # ========================================================
    # TEST 2 — Policy must block contact
    # ========================================================

    policy_result = (
        tracker.policy_engine.check_action(
            state=state,
            action="whatsapp",
            now=base_time,
        )
    )

    print()
    print(
        "TEST 2 — Policy integration"
    )

    print(
        f"  WhatsApp allowed: "
        f"{policy_result.allowed}"
    )

    for reason in (
        policy_result.blocking_reasons
    ):

        print(
            f"  BLOCK: {reason}"
        )

    assert (
        policy_result.allowed
        is False
    )

    assert any(
        "Active promise-to-pay"
        in reason
        for reason
        in policy_result.blocking_reasons
    )

    # ========================================================
    # TEST 3 — Mark promise paid
    # ========================================================

    paid = tracker.mark_paid(
        case_id="RV-PTP-001",
        paid_at=datetime(
            2026,
            9,
            1,
            11,
            0,
        ),
        payment_reference="pay_test_001",
        payment_source="razorpay_webhook",
        payment_verified=True,
    )

    print()
    print("TEST 3 — Promise kept")

    print(
        f"  Status:     "
        f"{paid.status.value}"
    )

    print(
        f"  Payment:    "
        f"{paid.payment_reference}"
    )

    print(
        f"  Source:     "
        f"{paid.payment_source}"
    )

    print(
        f"  Verified:   "
        f"{paid.payment_verified}"
    )

    state = tracker.get_state(
        "RV-PTP-001"
    )

    print(
        f"  Active PTP: "
        f"{state.promise_to_pay_active}"
    )

    assert (
        paid.status
        == PromiseStatus.PAID
    )

    assert (
        paid.payment_reference
        == "pay_test_001"
    )

    assert (
        paid.payment_source
        == "razorpay_webhook"
    )

    assert (
        paid.payment_verified
        is True
    )

    assert (
        state.promise_to_pay_active
        is False
    )

    # ========================================================
    # TEST 4 — Policy after payment
    # ========================================================

    policy_result = (
        tracker.policy_engine.check_action(
            state=state,
            action="whatsapp",
            now=datetime(
                2026,
                9,
                1,
                12,
                0,
            ),
        )
    )

    print()
    print(
        "TEST 4 — Policy after payment"
    )

    print(
        f"  WhatsApp allowed: "
        f"{policy_result.allowed}"
    )

    assert (
        policy_result.allowed
        is True
    )

    # ========================================================
    # TEST 5 — Broken promise
    # ========================================================

    broken_tracker = PromiseTracker(
        persistence_enabled=False
    )

    broken_tracker.create_promise(
        case_id="RV-PTP-002",
        promised_amount=75000,
        promise_date=datetime(
            2026,
            9,
            1,
            10,
            0,
        ),
        created_at=base_time,
        customer_id="cust-002",
        customer_name="Broken Promise Corp",
        invoice_id="INV-PTP-002",
    )

    broken = broken_tracker.mark_broken(
        case_id="RV-PTP-002",
        broken_at=datetime(
            2026,
            9,
            2,
            10,
            0,
        ),
    )

    print()
    print("TEST 5 — Broken promise")

    print(
        f"  Status:     "
        f"{broken.status.value}"
    )

    print(
        f"  Reason:     "
        f"{broken.reason}"
    )

    broken_state = (
        broken_tracker.get_state(
            "RV-PTP-002"
        )
    )

    print(
        f"  Active PTP: "
        f"{broken_state.promise_to_pay_active}"
    )

    assert (
        broken.status
        == PromiseStatus.BROKEN
    )

    assert (
        broken_state.promise_to_pay_active
        is False
    )

    # ========================================================
    # TEST 6 — Broken promise can be reconsidered
    # ========================================================

    policy_result = (
        broken_tracker.policy_engine.check_action(
            state=broken_state,
            action="voice_call",
            now=datetime(
                2026,
                9,
                2,
                11,
                0,
            ),
        )
    )

    print()
    print(
        "TEST 6 — Re-escalation eligibility"
    )

    print(
        f"  Voice call allowed: "
        f"{policy_result.allowed}"
    )

    assert (
        policy_result.allowed
        is True
    )

    # ========================================================
    # TEST 7 — Automatic due-promise evaluation
    # ========================================================

    due_tracker = PromiseTracker(
        persistence_enabled=False
    )

    due_tracker.create_promise(
        case_id="RV-PTP-003",
        promised_amount=25000,
        promise_date=datetime(
            2026,
            8,
            30,
            10,
            0,
        ),
        created_at=base_time,
    )

    due = (
        due_tracker.evaluate_due_promises(
            now=datetime(
                2026,
                8,
                31,
                10,
                0,
            )
        )
    )

    print()
    print(
        "TEST 7 — Due promise evaluation"
    )

    print(
        f"  Broken promises found: "
        f"{len(due)}"
    )

    assert len(due) == 1

    assert (
        due[0].status
        == PromiseStatus.BROKEN
    )

    # ========================================================
    # TEST 8 — Invalid promise amount
    # ========================================================

    invalid_tracker = PromiseTracker(
        persistence_enabled=False
    )

    print()
    print(
        "TEST 8 — Invalid promise amount"
    )

    try:

        invalid_tracker.create_promise(
            case_id="RV-PTP-004",
            promised_amount=0,
            promise_date=datetime(
                2026,
                9,
                2,
            ),
            created_at=base_time,
        )

        raise AssertionError(
            "Invalid promise amount was accepted."
        )

    except ValueError as error:

        print(
            f"  ✓ Rejected: {error}"
        )

    # ========================================================
    # TEST 9 — Duplicate active promise
    # ========================================================

    duplicate_tracker = PromiseTracker(
        persistence_enabled=False
    )

    duplicate_tracker.create_promise(
        case_id="RV-PTP-005",
        promised_amount=10000,
        promise_date=datetime(
            2026,
            9,
            2,
        ),
        created_at=base_time,
    )

    print()
    print(
        "TEST 9 — Duplicate active promise"
    )

    try:

        duplicate_tracker.create_promise(
            case_id="RV-PTP-005",
            promised_amount=15000,
            promise_date=datetime(
                2026,
                9,
                3,
            ),
            created_at=base_time,
        )

        raise AssertionError(
            "Duplicate active promise was accepted."
        )

    except ValueError as error:

        print(
            f"  ✓ Rejected: {error}"
        )

    # ========================================================
    # TEST 10 — Audit trail
    # ========================================================

    audit = tracker.get_audit_trail(
        "RV-PTP-001"
    )

    print()
    print(
        "TEST 10 — Audit trail"
    )

    for transition in audit:

        print(
            f"  {transition.previous_status.value}"
            f" → "
            f"{transition.new_status.value}"
            f" | "
            f"{transition.reason}"
        )

    assert len(audit) == 2

    assert all(
        transition.promise_id
        == promise.promise_id
        for transition in audit
    )

    # ========================================================
    # TEST 11 — Historical record retained
    # ========================================================

    history = tracker.get_promise_history(
        "RV-PTP-001"
    )

    print()
    print(
        "TEST 11 — Historical promise retention"
    )

    print(
        f"  Historical records: "
        f"{len(history)}"
    )

    assert len(history) >= 1

    # ========================================================
    # TEST 12 — Persistence round-trip
    # ========================================================

    persistence_file = (
        Path(__file__).resolve().parent
        / "_promise_tracker_selftest.json"
    )

    if persistence_file.exists():

        persistence_file.unlink()

    persistent_tracker = PromiseTracker(
        storage_path=persistence_file,
        persistence_enabled=True,
    )

    persistent_promise = (
        persistent_tracker.create_promise(
            case_id="RV-PTP-PERSIST-001",
            promised_amount=12000,
            promise_date=datetime(
                2026,
                9,
                5,
                12,
                0,
            ),
            created_at=base_time,
            customer_id="cust-persist",
            customer_name="Persistence Test",
            invoice_id="INV-PERSIST-001",
            original_amount=15000,
            outstanding_amount=12000,
        )
    )

    # Create a completely new tracker instance.
    restored_tracker = PromiseTracker(
        storage_path=persistence_file,
        persistence_enabled=True,
    )

    restored = (
        restored_tracker.get_promise(
            "RV-PTP-PERSIST-001"
        )
    )

    print()
    print(
        "TEST 12 — Persistence round-trip"
    )

    print(
        f"  Stored promise: "
        f"{persistent_promise.promise_id}"
    )

    print(
        f"  Restored promise: "
        f"{restored.promise_id if restored else None}"
    )

    assert restored is not None

    assert (
        restored.promise_id
        == persistent_promise.promise_id
    )

    assert (
        restored.customer_id
        == "cust-persist"
    )

    assert (
        restored.invoice_id
        == "INV-PERSIST-001"
    )

    assert (
        restored.status
        == PromiseStatus.PROMISED
    )

    restored_state = (
        restored_tracker.get_state(
            "RV-PTP-PERSIST-001"
        )
    )

    assert (
        restored_state.promise_to_pay_active
        is True
    )

    # --------------------------------------------------------
    # Clean up self-test persistence file.
    # --------------------------------------------------------

    if persistence_file.exists():

        persistence_file.unlink()

    # ========================================================
    # TEST 13 — New promise after broken promise retains
    # historical record
    # ========================================================

    history_tracker = PromiseTracker(
        persistence_enabled=False
    )

    first = (
        history_tracker.create_promise(
            case_id="RV-PTP-HISTORY-001",
            promised_amount=10000,
            promise_date=datetime(
                2026,
                9,
                1,
                10,
                0,
            ),
            created_at=base_time,
        )
    )

    history_tracker.mark_broken(
        case_id="RV-PTP-HISTORY-001",
        broken_at=datetime(
            2026,
            9,
            2,
            10,
            0,
        ),
    )

    second = (
        history_tracker.create_promise(
            case_id="RV-PTP-HISTORY-001",
            promised_amount=9000,
            promise_date=datetime(
                2026,
                9,
                5,
                10,
                0,
            ),
            created_at=datetime(
                2026,
                9,
                2,
                11,
                0,
            ),
        )
    )

    historical = (
        history_tracker.get_promise_history(
            "RV-PTP-HISTORY-001"
        )
    )

    print()
    print(
        "TEST 13 — Promise history across re-promise"
    )

    print(
        f"  First promise:  {first.promise_id}"
    )

    print(
        f"  Second promise: {second.promise_id}"
    )

    print(
        f"  History count:  {len(historical)}"
    )

    assert (
        first.promise_id
        != second.promise_id
    )

    assert any(
        record.promise_id
        == first.promise_id
        for record in historical
    )

    assert (
        history_tracker.get_status(
            "RV-PTP-HISTORY-001"
        )
        == PromiseStatus.PROMISED
    )

    # ========================================================
    # Metrics
    # ========================================================

    metrics = tracker.metrics()

    print()
    print(
        "Promise metrics:"
    )

    print(
        f"  Total promises:     "
        f"{metrics['total_promises']}"
    )

    print(
        f"  Active promises:    "
        f"{metrics['active_promises']}"
    )

    print(
        f"  Promises kept:      "
        f"{metrics['promises_kept']}"
    )

    print(
        f"  Promises broken:    "
        f"{metrics['promises_broken']}"
    )

    print(
        f"  Promise kept rate:  "
        f"{metrics['promise_kept_rate']:.2%}"
    )

    print(
        f"  Historical records: "
        f"{metrics['historical_promises']}"
    )

    print(
        f"  Audit transitions:  "
        f"{metrics['audit_transitions']}"
    )

    # ========================================================
    # Final
    # ========================================================

    print()
    print("=" * 72)
    print("MODULE 5 SELF-TEST: PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()