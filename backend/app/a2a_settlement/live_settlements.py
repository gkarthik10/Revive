"""
Revive Live A2A Settlement Store

Persistent state for real/live A2A settlement agreements.

IMPORTANT:

AGREED != PAYMENT_CONFIRMED

A2A negotiation creates an agreement.

Razorpay payment.captured is what confirms
actual payment and recovery.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# Storage
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = BASE_DIR / "data"

LIVE_SETTLEMENTS_FILE = (
    DATA_DIR / "live_a2a_settlements.json"
)


# ============================================================
# Helpers
# ============================================================


def _utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# Store
# ============================================================


class LiveA2ASettlementStore:
    """
    Thread-safe persistent store for live A2A agreements.

    One record represents one settlement agreement and
    its payment lifecycle.
    """

    def __init__(
        self,
        file_path: Path | None = None,
    ) -> None:

        self.file_path = (
            file_path
            or LIVE_SETTLEMENTS_FILE
        )

        self._lock = (
            threading.RLock()
        )

        self._ensure_file()

    # ========================================================
    # File handling
    # ========================================================

    def _ensure_file(self) -> None:

        self.file_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if not self.file_path.exists():

            self.file_path.write_text(
                "[]",
                encoding="utf-8",
            )

    def _read(self) -> list[dict[str, Any]]:

        with self._lock:

            self._ensure_file()

            try:

                raw = (
                    self.file_path.read_text(
                        encoding="utf-8"
                    )
                )

                data = json.loads(
                    raw
                )

            except (
                OSError,
                json.JSONDecodeError,
            ):

                return []

            if not isinstance(
                data,
                list,
            ):

                return []

            return [
                item
                for item in data
                if isinstance(
                    item,
                    dict,
                )
            ]

    def _write(
        self,
        records: list[dict[str, Any]],
    ) -> None:
        """
        Persist records to the live settlement file.

        IMPORTANT:

        The application runs inside Docker while the data file
        is bind-mounted from the Windows host.

        Atomic os.replace()/rename operations on this bind mount
        can fail with:

            [Errno 16] Device or resource busy

        Therefore records are written directly to the mounted
        JSON file instead of replacing the file via rename.
        """

        with self._lock:

            self._ensure_file()

            payload = json.dumps(
                records,
                indent=2,
                ensure_ascii=False,
            )

            self.file_path.write_text(
                payload,
                encoding="utf-8",
            )

    # ========================================================
    # Create agreement
    # ========================================================

    def create(
        self,
        *,
        case_id: str,
        invoice_id: str,
        revive_case_tag: str,
        agreed_amount: float,
        payer_agent_id: str,
        agreement_id: str | None = None,
        a2a_task_id: str | None = None,
        a2a_context_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a live A2A settlement agreement.

        The agreement starts as:

            settlement_status = AGREED
            payment_status = PENDING
            recovery_confirmed = False
        """

        if agreed_amount <= 0:

            raise ValueError(
                "Agreed settlement amount must be greater than zero."
            )

        with self._lock:

            records = self._read()

            # ------------------------------------------------
            # Idempotency by agreement ID.
            # ------------------------------------------------

            if agreement_id:

                for record in records:

                    if (
                        record.get(
                            "agreement_id"
                        )
                        == agreement_id
                    ):

                        return dict(
                            record
                        )

            # ------------------------------------------------
            # Generate ID if necessary.
            # ------------------------------------------------

            final_agreement_id = (
                agreement_id
                or (
                    "AGR-"
                    + uuid.uuid4().hex[:16].upper()
                )
            )

            now = _utc_now()

            record = {
                "agreement_id": (
                    final_agreement_id
                ),

                "case_id": str(
                    case_id
                ),

                "invoice_id": str(
                    invoice_id
                ),

                "revive_case_tag": str(
                    revive_case_tag
                ),

                "payer_agent_id": str(
                    payer_agent_id
                ),

                "agreed_amount": round(
                    float(agreed_amount),
                    2,
                ),

                "settlement_status": "AGREED",

                "payment_status": "PENDING",

                "recovery_confirmed": False,

                "payment_link_id": None,

                "payment_url": None,

                "razorpay_payment_id": None,

                "created_at": now,

                "updated_at": now,

                "confirmed_at": None,

                "a2a_task_id": (
                    a2a_task_id
                ),

                "a2a_context_id": (
                    a2a_context_id
                ),
            }

            records.append(
                record
            )

            self._write(
                records
            )

            return dict(
                record
            )

    # ========================================================
    # Lookup
    # ========================================================

    def get_by_agreement_id(
        self,
        agreement_id: str,
    ) -> dict[str, Any] | None:

        with self._lock:

            for record in self._read():

                if (
                    record.get(
                        "agreement_id"
                    )
                    == agreement_id
                ):

                    return dict(
                        record
                    )

        return None

    def get_by_case_id(
        self,
        case_id: str,
    ) -> dict[str, Any] | None:

        with self._lock:

            matches = [
                record
                for record in self._read()
                if (
                    record.get(
                        "case_id"
                    )
                    == case_id
                )
            ]

            if not matches:

                return None

            return dict(
                matches[-1]
            )

    def get_by_case_tag(
        self,
        revive_case_tag: str,
    ) -> dict[str, Any] | None:

        with self._lock:

            matches = [
                record
                for record in self._read()
                if (
                    record.get(
                        "revive_case_tag"
                    )
                    == revive_case_tag
                )
            ]

            if not matches:

                return None

            return dict(
                matches[-1]
            )

    # ========================================================
    # Attach Razorpay payment link
    # ========================================================

    def attach_payment_link(
        self,
        agreement_id: str,
        *,
        payment_link_id: str,
        payment_url: str,
    ) -> dict[str, Any] | None:

        with self._lock:

            records = self._read()

            for record in records:

                if (
                    record.get(
                        "agreement_id"
                    )
                    != agreement_id
                ):

                    continue

                # --------------------------------------------
                # Do not replace an existing payment link.
                # --------------------------------------------

                if record.get(
                    "payment_link_id"
                ):

                    return dict(
                        record
                    )

                record[
                    "payment_link_id"
                ] = str(
                    payment_link_id
                )

                record[
                    "payment_url"
                ] = str(
                    payment_url
                )

                record[
                    "updated_at"
                ] = _utc_now()

                self._write(
                    records
                )

                return dict(
                    record
                )

        return None

    # ========================================================
    # Confirm payment
    # ========================================================

    def confirm_payment(
        self,
        *,
        revive_case_tag: str,
        razorpay_payment_id: str,
        recovered_amount: float,
        confirmed_at: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Mark an A2A agreement as payment-confirmed.

        This must only be called after a verified
        Razorpay payment.captured webhook.

        Financial integrity rule:

            captured amount == agreed amount

        A partial, excessive, malformed, or otherwise
        mismatched payment must NEVER confirm the A2A
        settlement.
        """

        if not razorpay_payment_id:

            return None

        # ----------------------------------------------------
        # Validate captured amount before touching state.
        # ----------------------------------------------------

        try:

            captured_amount = round(
                float(
                    recovered_amount
                ),
                2,
            )

        except (
            TypeError,
            ValueError,
        ):

            return None

        if captured_amount <= 0:

            return None

        with self._lock:

            records = self._read()

            matches = [
                record
                for record in records
                if (
                    record.get(
                        "revive_case_tag"
                    )
                    == revive_case_tag
                )
            ]

            if not matches:

                return None

            # ------------------------------------------------
            # Latest active agreement for the tag.
            # ------------------------------------------------

            record = matches[-1]

            # ------------------------------------------------
            # Validate agreed amount.
            # ------------------------------------------------

            try:

                agreed_amount = round(
                    float(
                        record.get(
                            "agreed_amount"
                        ) or 0
                    ),
                    2,
                )

            except (
                TypeError,
                ValueError,
            ):

                return None

            if agreed_amount <= 0:

                return None

            # ------------------------------------------------
            # Financial integrity check.
            #
            # A2A agreement:
            #
            #     ₹10,000
            #
            # Captured:
            #
            #     ₹1,000
            #
            # MUST NOT become confirmed.
            # ------------------------------------------------

            if captured_amount != agreed_amount:

                return None

            # ------------------------------------------------
            # Already confirmed.
            # ------------------------------------------------

            if record.get(
                "recovery_confirmed"
            ):

                # Same payment is idempotent.
                if (
                    record.get(
                        "razorpay_payment_id"
                    )
                    == razorpay_payment_id
                ):

                    return dict(
                        record
                    )

                # A different payment must never overwrite
                # an already-confirmed recovery.
                return dict(
                    record
                )

            # ------------------------------------------------
            # Confirm payment.
            # ------------------------------------------------

            record[
                "payment_status"
            ] = "CONFIRMED"

            record[
                "settlement_status"
            ] = "AGREED"

            record[
                "recovery_confirmed"
            ] = True

            record[
                "razorpay_payment_id"
            ] = str(
                razorpay_payment_id
            )

            timestamp = (
                confirmed_at
                or _utc_now()
            )

            record[
                "confirmed_at"
            ] = timestamp

            record[
                "updated_at"
            ] = timestamp

            self._write(
                records
            )

            return dict(
                record
            )

    # ========================================================
    # List
    # ========================================================

    def list_all(
        self,
    ) -> list[dict[str, Any]]:

        with self._lock:

            return [
                dict(record)
                for record in self._read()
            ]


# ============================================================
# Singleton
# ============================================================

live_a2a_settlement_store = (
    LiveA2ASettlementStore()
)