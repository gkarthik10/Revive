"""
Revive — Razorpay Live Payment Gateway Integration

Creates real Razorpay Payment Links, verifies Razorpay webhooks,
and converts payment.failed events into Revive live cases.

Environment variables:

    RAZORPAY_KEY_ID
    RAZORPAY_KEY_SECRET
    RAZORPAY_WEBHOOK_SECRET

For Razorpay Test Mode, use a matching:

    rzp_test_... key ID
    corresponding test-mode secret
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv


# ============================================================
# Environment
# ============================================================

# app/payments/razorpay_gateway.py
#
# parents[0] = payments
# parents[1] = app
# parents[2] = backend            (this is where `docker build` /
#                                   run_demo.sh's `cd backend` treat
#                                   as the working directory)
# parents[3] = revive/ project root (this is where the real .env
#                                     actually lives, alongside
#                                     docker-compose.yml)
#
# BUG FIX: this used to read `parents[2]` and call it "project
# root" — that's actually `backend/`, one level short. Docker
# never surfaced this because docker-compose's `env_file: - .env`
# injects the variables directly into the container's OS
# environment before Python even starts, so the broken lookup
# was a harmless no-op there. But `run_demo.sh` (the local,
# non-Docker path) has no such safety net: it relies entirely on
# this file finding `.env` itself, and previously it couldn't —
# meaning Razorpay checkout/live payments, and anything else read
# from `.env` after this import, would be silently missing.
#
# Fixed by checking both plausible locations (backend/.env, in
# case someone keeps a local copy there, and the real project
# root revive/.env) and loading whichever exists first.

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _BACKEND_DIR.parent

_ENV_FILE_CANDIDATES = (
    _BACKEND_DIR / ".env",
    _PROJECT_ROOT / ".env",
)

ENV_FILE = next(
    (path for path in _ENV_FILE_CANDIDATES if path.exists()),
    _PROJECT_ROOT / ".env",
)

# IMPORTANT:
# override=True ensures the project's .env is used even if
# stale Razorpay variables exist in the Windows environment.
load_dotenv(
    dotenv_path=ENV_FILE,
    override=True,
)


# ============================================================
# Constants
# ============================================================

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

LIVE_CASES_FILE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "live_cases.json"
)

_store_lock = threading.RLock()


# ============================================================
# Root Cause Mapping
# ============================================================

ERROR_REASON_TO_ROOT_CAUSE = {
    "payment_timed_out": "otp_timeout",
    "insufficient_fund": "insufficient_funds",
    "insufficient_funds": "insufficient_funds",
    "card_declined": "issuer_declined",
    "payment_declined": "issuer_declined",
    "payment_failed": "issuer_declined",
    "bank_processing_error": "network_error",
    "gateway_error": "network_error",
    "expired_card": "card_expired",
    "authentication_failed": "otp_timeout",
}

DEFAULT_ROOT_CAUSE = "issuer_declined"


# ============================================================
# Configuration
# ============================================================

class RazorpayConfigError(RuntimeError):
    """Raised when required Razorpay environment variables are missing."""


def _clean_env_value(
    value: str | None,
) -> str | None:
    """
    Clean an environment variable.

    Handles:

        value
        "value"
        'value'

    and removes surrounding whitespace.
    """

    if value is None:
        return None

    value = value.strip()

    if len(value) >= 2:

        if (
            value.startswith('"')
            and value.endswith('"')
        ) or (
            value.startswith("'")
            and value.endswith("'")
        ):
            value = value[1:-1].strip()

    return value or None


def _auth() -> tuple[str, str]:
    """
    Load and return the Razorpay API credentials.

    The actual secret is never printed or exposed.
    """

    key_id = _clean_env_value(
        os.environ.get("RAZORPAY_KEY_ID")
    )

    key_secret = _clean_env_value(
        os.environ.get("RAZORPAY_KEY_SECRET")
    )

    if not key_id or not key_secret:

        raise RazorpayConfigError(
            "Razorpay credentials are missing. "
            f"Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
            f"in {ENV_FILE}."
        )

    return key_id, key_secret


def _webhook_secret() -> str:
    """Load the Razorpay webhook secret."""

    secret = _clean_env_value(
        os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    )

    if not secret:

        raise RazorpayConfigError(
            "RAZORPAY_WEBHOOK_SECRET is missing. "
            f"Set it in {ENV_FILE}."
        )

    return secret


# ============================================================
# Checkout — Create Real Razorpay Payment Link
# ============================================================

def create_payment_link(
    amount_rupees: float,
    customer_name: str,
    customer_id: str,
    customer_email: str | None = None,
    customer_contact: str | None = None,
    description: str = "Revive recovery — payment link",
    revive_case_tag: str | None = None,
    surface: str = "subscription_failure",
    invoice_id: str | None = None,
    has_ap_agent: bool = False,
    disputed: bool = False,
    promise_id: str | None = None,
    reference_id: str | None = None,
    expire_by: int | None = None,
) -> dict[str, Any]:
    """
    Create a real Razorpay Payment Link.

    amount_rupees:
        Amount in INR.

    customer_name:
        Name shown on Razorpay checkout.

    customer_id:
        Revive customer ID stored in Razorpay notes.

    customer_email:
        Optional. When provided, attached to the Razorpay checkout
        customer object AND mirrored into notes.revive_customer_email
        so it survives the round trip on the payment.failed /
        payment.captured webhook and can be mapped back onto the
        Revive case / promise record.

    customer_contact:
        Optional. Phone number, same round-trip treatment as
        customer_email.

    revive_case_tag:
        When provided, this EXISTING tag is reused instead of
        generating a new one. This is what makes a retry
        checkout resolve the *same* live case on payment.captured
        instead of Razorpay's payment.captured handler treating it
        as an unmatched/new capture. Pass None for a brand-new
        (first-attempt) checkout.

    Returns:
        Razorpay Payment Link response.
    """

    key_id, key_secret = _auth()

    amount_rupees = float(amount_rupees)

    if amount_rupees <= 0:

        raise ValueError(
            "Payment amount must be greater than zero."
        )

    tag = (
        revive_case_tag
        or f"revive-{uuid.uuid4().hex[:12]}"
    )

    payload = {
        "amount": round(
            amount_rupees * 100
        ),
        "currency": "INR",
        "description": description,
        "customer": {
            "name": customer_name,
            **(
                {"email": str(customer_email).strip()}
                if customer_email
                else {}
            ),
            **(
                {"contact": str(customer_contact).strip()}
                if customer_contact
                else {}
            ),
        },
        "notes": {
            "revive_case_tag": tag,
            "revive_customer_id": customer_id,
            "revive_customer_name": customer_name,
            "revive_customer_email": (
                str(customer_email).strip()
                if customer_email
                else ""
            ),
            "revive_customer_contact": (
                str(customer_contact).strip()
                if customer_contact
                else ""
            ),
            "revive_surface": str(surface),
            "revive_invoice_id": (
                str(invoice_id)
                if invoice_id
                else ""
            ),
            "revive_has_ap_agent": (
                "true"
                if has_ap_agent
                else "false"
            ),
            "revive_disputed": (
                "true"
                if disputed
                else "false"
            ),
            "revive_promise_id": (
                str(promise_id)
                if promise_id
                else ""
            ),
        },
    }

    if reference_id:
        payload["reference_id"] = str(reference_id)[:40]

    if expire_by is not None:
        payload["expire_by"] = int(expire_by)

    try:

        response = requests.post(
            f"{RAZORPAY_API_BASE}/payment_links",
            auth=(
                key_id,
                key_secret,
            ),
            json=payload,
            timeout=15,
        )

    except requests.RequestException as exc:

        raise RuntimeError(
            f"Unable to connect to Razorpay: {exc}"
        ) from exc


    # ========================================================
    # Authentication Failure
    # ========================================================

    if response.status_code == 401:

        raise RuntimeError(
            "Razorpay authentication failed (HTTP 401). "
            "The Razorpay API rejected the credentials being "
            "used by this FastAPI process. Verify that "
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are the "
            "matching pair from the same Razorpay mode."
        )


    # ========================================================
    # Other Razorpay Errors
    # ========================================================

    if response.status_code >= 300:

        raise RuntimeError(
            "Razorpay rejected the payment link request "
            f"(HTTP {response.status_code}): "
            f"{response.text}"
        )


    # ========================================================
    # Parse Response
    # ========================================================

    try:

        result = response.json()

    except ValueError as exc:

        raise RuntimeError(
            "Razorpay returned an invalid JSON response."
        ) from exc


    # ========================================================
    # Validate Payment Link Response
    # ========================================================

    if not result.get("id"):

        raise RuntimeError(
            "Razorpay response did not contain a payment "
            "link ID."
        )

    if not result.get("short_url"):

        raise RuntimeError(
            "Razorpay response did not contain a checkout URL."
        )


    # ========================================================
    # Revive Metadata
    # ========================================================

    result["revive_case_tag"] = tag

    return result


# ============================================================
# Webhook Signature Verification
# ============================================================

def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str | None = None,
) -> bool:
    """
    Verify a Razorpay webhook request.

    Razorpay signs the RAW request body with HMAC-SHA256.

    The comparison uses hmac.compare_digest() to avoid
    timing-based comparison attacks.
    """

    if not signature_header:

        return False

    try:

        resolved_secret = (
            _clean_env_value(secret)
            if secret
            else _webhook_secret()
        )

    except RazorpayConfigError:

        return False

    if not resolved_secret:

        return False

    expected = hmac.new(
        resolved_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        signature_header.strip(),
    )


# ============================================================
# Razorpay Webhook → Revive Case
# ============================================================

def map_failed_payment_to_case(
    event_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert a real Razorpay payment.failed webhook into a
    LIVE Revive recovery case.

    IMPORTANT:
    A failed Razorpay payment is NOT a recovered case.

    The live case starts in:
        outcome = UNRECOVERED
        recovery_status = PENDING_RECOVERY
        recovered_amount = 0

    Only an actual later successful payment may transition
    this case to RECOVERED.
    """

    if event_payload.get("event") != "payment.failed":
        return None

    payment = (
        event_payload
        .get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    if not payment:
        return None

    payment_id = payment.get("id")

    if not payment_id:
        return None

    # --------------------------------------------------------
    # Failure reason
    # --------------------------------------------------------

    reason = str(
        payment.get("error_reason")
        or ""
    ).strip().lower()

    root_cause = ERROR_REASON_TO_ROOT_CAUSE.get(
        reason,
        DEFAULT_ROOT_CAUSE,
    )

    # --------------------------------------------------------
    # Razorpay notes
    # --------------------------------------------------------

    notes = payment.get("notes") or {}

    if not isinstance(notes, dict):
        notes = {}

    revive_case_tag = (
        notes.get("revive_case_tag")
        or ""
    )

    customer_id = (
        notes.get("revive_customer_id")
        or "cust_live_unknown"
    )

    customer_name = (
        notes.get("revive_customer_name")
        or "Live Checkout Customer"
    )

    # --------------------------------------------------------
    # Customer contact details.
    #
    # Prefer the value Revive itself attached (notes), since that
    # is guaranteed to be the value the customer was actually
    # contacted through. Fall back to whatever Razorpay captured
    # natively on the payment entity (Razorpay always collects an
    # email/contact at checkout, independent of notes) so a live
    # case still carries a usable email even for payments created
    # outside Revive's own payment-link flow.
    # --------------------------------------------------------

    customer_email = (
        notes.get("revive_customer_email")
        or payment.get("email")
        or None
    )

    customer_contact = (
        notes.get("revive_customer_contact")
        or payment.get("contact")
        or None
    )

    surface = str(
        notes.get("revive_surface")
        or "subscription_failure"
    )

    invoice_id_raw = notes.get(
        "revive_invoice_id"
    )

    invoice_id = (
        str(invoice_id_raw)
        if invoice_id_raw
        else None
    )

    has_ap_agent = (
        str(
            notes.get(
                "revive_has_ap_agent",
                "false",
            )
        ).strip().lower()
        == "true"
    )

    disputed = (
        str(
            notes.get(
                "revive_disputed",
                "false",
            )
        ).strip().lower()
        == "true"
    )

    # --------------------------------------------------------
    # Amount
    # --------------------------------------------------------

    amount_rupees = round(
        float(payment.get("amount") or 0) / 100,
        2,
    )

    # --------------------------------------------------------
    # Payment method details
    # --------------------------------------------------------

    card = payment.get("card") or {}

    payment_method = (
        payment.get("method")
        or "unknown"
    )

    bank = (
        payment.get("bank")
        or payment.get("wallet")
        or "unknown"
    )

    card_network = (
        card.get("network")
        if isinstance(card, dict) and card
        else payment_method
    )

    # --------------------------------------------------------
    # Case ID
    # --------------------------------------------------------

    case_id = f"RV-LIVE-{payment_id}"

    # --------------------------------------------------------
    # IMPORTANT:
    # This is an observed failure, NOT a simulated recovery.
    # --------------------------------------------------------

    return {
        "case_id": case_id,

        "surface": surface,

        "invoice_id": invoice_id,

        "has_ap_agent": has_ap_agent,

        "disputed": disputed,

        "customer_id": str(customer_id),

        "customer_name": str(customer_name),

        "customer_email": (
            str(customer_email)
            if customer_email
            else None
        ),

        "customer_contact": (
            str(customer_contact)
            if customer_contact
            else None
        ),

        "amount": amount_rupees,

        # Current capture/failure timestamp in IST.
        "timestamp": datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).isoformat(),

        # Original Razorpay event timestamp.
        "razorpay_created_at": payment.get(
            "created_at"
        ),

        "root_cause_label": root_cause,

        "decline_code": root_cause,

        "bank": bank,

        "card_network": card_network,

        "payment_method": payment_method,

        "customer_tenure_days": None,

        "retry_count": 0,

        # ----------------------------------------------------
        # LIVE RECOVERY STATE
        # ----------------------------------------------------

        "outcome": "UNRECOVERED",

        "recovery_status": "PENDING_RECOVERY",

        "recovered_amount": 0.0,

        "recovery_source": None,

        "recovered_at": None,

        # ----------------------------------------------------
        # Provenance
        # ----------------------------------------------------

        "source": "razorpay_live_webhook",

        "is_live": True,

        "razorpay_payment_id": payment_id,

        "revive_case_tag": revive_case_tag,

        "razorpay_raw_error": {
            "error_code": payment.get(
                "error_code"
            ),
            "error_description": payment.get(
                "error_description"
            ),
            "error_reason": payment.get(
                "error_reason"
            ),
            "error_source": payment.get(
                "error_source"
            ),
            "error_step": payment.get(
                "error_step"
            ),
        },
    }

def map_captured_payment_to_recovery(
    event_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert a real Razorpay payment.captured webhook into
    a recovery update.

    Only payments carrying a Revive case tag are considered.

    A normal successful Razorpay payment that has no Revive
    metadata is ignored.
    """

    if event_payload.get("event") != "payment.captured":
        return None

    payment = (
        event_payload
        .get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    if not payment:
        return None

    payment_id = payment.get("id")

    if not payment_id:
        return None

    notes = payment.get("notes") or {}

    if not isinstance(notes, dict):
        notes = {}

    revive_case_tag = (
        notes.get("revive_case_tag")
        or ""
    )

    if not revive_case_tag:
        return None

    amount_rupees = round(
        float(payment.get("amount") or 0) / 100,
        2,
    )

    # Carry the paying customer's email/contact forward too, using
    # the same notes-first-then-native-payment-field precedence as
    # map_failed_payment_to_case, so downstream reconciliation
    # (promise alerts, recovery ledger, dashboard) can always
    # resolve who actually paid, not just how much.
    customer_email = (
        notes.get("revive_customer_email")
        or payment.get("email")
        or None
    )

    customer_contact = (
        notes.get("revive_customer_contact")
        or payment.get("contact")
        or None
    )

    return {
        "revive_case_tag": revive_case_tag,
        "razorpay_payment_id": payment_id,
        "recovered_amount": amount_rupees,
        "recovered_at": datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).isoformat(),
        "customer_email": (
            str(customer_email)
            if customer_email
            else None
        ),
        "customer_contact": (
            str(customer_contact)
            if customer_contact
            else None
        ),
    }


def map_captured_payment_to_new_case(
    event_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Build a brand-new, already-RECOVERED live case directly from a
    payment.captured webhook.

    WHY THIS EXISTS:

    mark_recovered() (used by map_captured_payment_to_recovery's
    caller) can only ever UPDATE a case that already exists in the
    live case store, matched by revive_case_tag. That case is only
    ever created on a prior payment.failed event.

    A payment that succeeds on its FIRST attempt never went through
    payment.failed, so there is no existing case to attach the
    recovery to. Without this function, that capture is silently
    dropped ("ignored": true) and never appears on the dashboard,
    even though the payment genuinely succeeded.

    This function builds that missing case directly, already marked
    RECOVERED, using the same Revive metadata (notes) that
    create_payment_link() attaches at checkout time. Only payments
    carrying a revive_case_tag are considered — a normal Razorpay
    payment with no Revive metadata returns None and is ignored, same
    as map_captured_payment_to_recovery.
    """

    payment = (
        event_payload
        .get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    if not payment:
        return None

    payment_id = payment.get("id")

    if not payment_id:
        return None

    notes = payment.get("notes") or {}

    if not isinstance(notes, dict):
        notes = {}

    revive_case_tag = (
        notes.get("revive_case_tag")
        or ""
    )

    if not revive_case_tag:
        return None

    customer_id = (
        notes.get("revive_customer_id")
        or "cust_live_unknown"
    )

    customer_name = (
        notes.get("revive_customer_name")
        or "Live Checkout Customer"
    )

    customer_email = (
        notes.get("revive_customer_email")
        or payment.get("email")
        or None
    )

    customer_contact = (
        notes.get("revive_customer_contact")
        or payment.get("contact")
        or None
    )

    surface = str(
        notes.get("revive_surface")
        or "subscription_failure"
    )

    invoice_id_raw = notes.get(
        "revive_invoice_id"
    )

    invoice_id = (
        str(invoice_id_raw)
        if invoice_id_raw
        else None
    )

    has_ap_agent = (
        str(
            notes.get(
                "revive_has_ap_agent",
                "false",
            )
        ).strip().lower()
        == "true"
    )

    disputed = (
        str(
            notes.get(
                "revive_disputed",
                "false",
            )
        ).strip().lower()
        == "true"
    )

    amount_rupees = round(
        float(payment.get("amount") or 0) / 100,
        2,
    )

    card = payment.get("card") or {}

    payment_method = (
        payment.get("method")
        or "unknown"
    )

    bank = (
        payment.get("bank")
        or payment.get("wallet")
        or "unknown"
    )

    card_network = (
        card.get("network")
        if isinstance(card, dict) and card
        else payment_method
    )

    case_id = f"RV-LIVE-{payment_id}"

    now_iso = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).isoformat()

    return {
        "case_id": case_id,

        "surface": surface,

        "invoice_id": invoice_id,

        "has_ap_agent": has_ap_agent,

        "disputed": disputed,

        "customer_id": str(customer_id),

        "customer_name": str(customer_name),

        "customer_email": (
            str(customer_email)
            if customer_email
            else None
        ),

        "customer_contact": (
            str(customer_contact)
            if customer_contact
            else None
        ),

        "amount": amount_rupees,

        "timestamp": now_iso,

        "razorpay_created_at": payment.get(
            "created_at"
        ),

        "root_cause_label": None,

        "decline_code": None,

        "bank": bank,

        "card_network": card_network,

        "payment_method": payment_method,

        "customer_tenure_days": None,

        "retry_count": 0,

        # ----------------------------------------------------
        # LIVE RECOVERY STATE
        #
        # Created directly as RECOVERED: this case represents a
        # payment that succeeded on its first attempt, so there
        # was never an UNRECOVERED/PENDING_RECOVERY stage to pass
        # through.
        # ----------------------------------------------------

        "outcome": "RECOVERED",

        "recovery_status": "RECOVERED",

        "recovered_amount": amount_rupees,

        "recovery_source": "razorpay_payment_captured",

        "recovered_at": now_iso,

        "updated_at": now_iso,

        # ----------------------------------------------------
        # Provenance
        # ----------------------------------------------------

        "source": "razorpay_live_webhook_direct_capture",

        "is_live": True,

        "razorpay_payment_id": payment_id,

        "recovery_payment_id": payment_id,

        "revive_case_tag": revive_case_tag,

        "razorpay_raw_error": None,
    }


# ============================================================
# Live Case Store
# ============================================================

class LiveCaseStore:
    """
    Thread-safe append-only JSON store for real Razorpay
    webhook-captured cases.
    """

    def __init__(
        self,
        path: Path = LIVE_CASES_FILE,
    ) -> None:

        self.path = path


    def load(self) -> list[dict[str, Any]]:
        """Load all stored live cases."""

        if not self.path.exists():

            return []

        with _store_lock:

            try:

                with self.path.open(
                    "r",
                    encoding="utf-8",
                ) as file:

                    data = json.load(file)

            except (
                json.JSONDecodeError,
                OSError,
            ):

                return []


        if isinstance(data, list):

            return data

        return []


    def add(
        self,
        case: dict[str, Any],
    ) -> bool:
        """
        Add a live case.

        Duplicate Razorpay webhook deliveries are ignored
        using case_id.
        """

        with _store_lock:

            cases = self.load()

            existing_ids = {
                item.get("case_id")
                for item in cases
            }

            if case["case_id"] in existing_ids:

                return False


            cases.append(case)

            self.path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self.path.open(
                "w",
                encoding="utf-8",
            ) as file:

                json.dump(
                    cases,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

            return True


    def clear(self) -> None:
        """Delete all live webhook-captured cases."""

        with _store_lock:

            if self.path.exists():

                self.path.unlink()

    def find_by_case_id(
        self,
        case_id: str,
    ) -> dict[str, Any] | None:
        """Look up a single live case by its Revive case_id."""

        for case in self.load():

            if case.get("case_id") == case_id:

                return case

        return None

    def record_retry_link(
        self,
        case_id: str,
        short_url: str,
    ) -> dict[str, Any] | None:
        """
        Record that a new retry Payment Link was issued for an
        existing PENDING_RECOVERY case. Does NOT mark the case
        recovered — only payment.captured can do that (see
        mark_recovered). Increments retry_count for visibility.
        """

        with _store_lock:

            cases = self.load()

            target = None

            for case in cases:

                if case.get("case_id") == case_id:
                    target = case
                    break

            if target is None:
                return None

            target["retry_count"] = (
                int(target.get("retry_count") or 0) + 1
            )

            target["last_retry_short_url"] = short_url

            with self.path.open(
                "w",
                encoding="utf-8",
            ) as file:

                json.dump(
                    cases,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

            return target

    def mark_recovered(
    self,
    revive_case_tag: str,
    recovered_amount: float,
    razorpay_payment_id: str,
    recovered_at: str,
    ) -> dict[str, Any] | None:
        """
        Mark an existing live failure as genuinely recovered.

        Recovery is only possible for a previously captured live
        failure carrying the same Revive case tag.
        """

        with _store_lock:

            cases = self.load()

            target = None

            for case in cases:

                if (
                    case.get("revive_case_tag")
                    == revive_case_tag
                ):
                    target = case
                    break

            if target is None:
                return None

            # --------------------------------------------------
            # IDEMPOTENCY (spec section 7):
            # Razorpay may redeliver the same payment.captured
            # event. If this exact payment already recovered this
            # case, return the existing state unchanged instead of
            # re-writing recovered_at / re-triggering downstream
            # effects a second time.
            # --------------------------------------------------

            if (
                target.get("recovery_status") == "RECOVERED"
                and target.get("recovery_payment_id")
                == razorpay_payment_id
            ):
                return target

            original_amount = float(
                target.get("amount") or 0
            )

            actual_recovery = min(
                max(float(recovered_amount), 0.0),
                original_amount,
            )

            target["outcome"] = "RECOVERED"

            target["recovery_status"] = "RECOVERED"

            target["recovered_amount"] = actual_recovery

            target["recovery_source"] = (
                "razorpay_payment_captured"
            )

            target["recovered_at"] = recovered_at

            target["recovery_payment_id"] = (
                razorpay_payment_id
            )

            target["updated_at"] = recovered_at

            self.path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self.path.open(
                "w",
                encoding="utf-8",
            ) as file:

                json.dump(
                    cases,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

            return target


# ============================================================
# Singleton Live Case Store
# ============================================================

live_case_store = LiveCaseStore()