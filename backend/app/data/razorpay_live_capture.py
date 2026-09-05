"""
Revive — Real Razorpay Test-Mode Capture (optional, standalone)

Everything else in this dataset is synthetic. This script creates ONE
real payment link on Razorpay's actual test-mode sandbox, walks you
through failing it, and captures the REAL error response Razorpay's
own system returns — not a simulated one.

WHY THIS IS SEPARATE FROM cases.json / cases.csv:

    Several self-tests across this codebase (pipeline.py, roi.py,
    test_roi.py) hard-assert the dataset is exactly 105 cases. Merging
    a 106th case into cases.json would break those currently-passing
    tests for a "nice to have" credibility feature — a bad trade this
    close to a deadline. So this script writes its result to its own
    file, `real_captured_case.json`, which you can show separately in
    your README or pitch ("here's a failure we actually triggered
    against Razorpay's sandbox") without touching the verified batch.

WHAT YOU NEED BEFORE RUNNING THIS:

    1. A free Razorpay account (razorpay.com -> Sign Up).
    2. Test-mode API keys: Dashboard -> Settings -> API Keys, with the
       "Test Mode" toggle ON. Copy the Key Id (starts with rzp_test_)
       and generate a Key Secret.
    3. Set them as environment variables before running:

           export RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
           export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx

       (On Windows PowerShell: $env:RAZORPAY_KEY_ID = "rzp_test_...")

    4. `pip install requests` if it isn't already installed.

HOW IT WORKS:

    1. Creates a real Payment Link via Razorpay's Orders/Payment Links
       API — this alone is a genuine, verifiable Razorpay sandbox
       artifact (real id, real timestamps).
    2. Prints the link and opens it in your browser.
    3. You complete ONE manual step in the browser: Razorpay's test
       mode requires an explicit click to fail a transaction (this
       is how their sandbox is designed — there is no pure
       server-to-server way to force a card decline). The fastest,
       most reliable way to do this:

           Choose UPI as the payment method
           Enter the UPI ID: failure@razorpay
           Confirm

       This is Razorpay's own documented deterministic failure
       trigger. Alternatively, choose Card, use one of Razorpay's
       published test-failure card numbers (check razorpay.com/docs
       for the current list, since they do update these), and click
       "Failure" on the mock bank page when prompted.

    4. This script polls Razorpay's Payments API in the background
       and detects your completed (failed) payment automatically by
       matching a unique tag it embedded in the payment link's notes
       at creation time.
    5. Once found, it fetches the full payment record — including
       Razorpay's REAL error_code, error_description, and
       error_reason — maps that onto Revive's root-cause taxonomy,
       and saves it to real_captured_case.json.

I have not been able to run this end-to-end myself: this sandbox has
no network access to api.razorpay.com. The Razorpay API shapes used
here (Payment Links, Payments list/fetch, the notes field, the
documented failure@razorpay UPI trigger) are all drawn from Razorpay's
public docs, but please run this once yourself and read the printed
output carefully rather than assuming it works blind — the same way
you'd sanity-check anything before a live demo.

Run: python3 razorpay_live_capture.py
"""

from __future__ import annotations

import json
import os
import time
import uuid
import webbrowser
from pathlib import Path

import requests

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
OUTPUT_FILE = Path(__file__).resolve().parent / "real_captured_case.json"

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300  # 5 minutes to complete the manual step

# Amount to request — kept small since this is test mode and no real
# money moves, but Razorpay still requires a minimum (typically ₹1).
CAPTURE_AMOUNT_RUPEES = 499

# Best-effort mapping from Razorpay's real error_reason values to
# Revive's root-cause taxonomy. Razorpay's exact reason strings can
# vary by payment method and may be updated over time — if the reason
# you get back isn't in this dict, the script will tell you plainly
# instead of silently guessing, so you can extend this mapping with
# the real value you observed.
ERROR_REASON_TO_ROOT_CAUSE = {
    "payment_timed_out": "otp_timeout",
    "insufficient_fund": "insufficient_funds",
    "insufficient_funds": "insufficient_funds",
    "card_declined": "issuer_declined",
    "payment_declined": "issuer_declined",
    "bank_processing_error": "network_error",
    "gateway_error": "network_error",
    "expired_card": "card_expired",
    "authentication_failed": "otp_timeout",
}


def _auth():
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")

    if not key_id or not key_secret:
        raise SystemExit(
            "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test-mode "
            "keys, key id starts with rzp_test_) before running this. "
            "See the module docstring for where to get them."
        )

    if not key_id.startswith("rzp_test_"):
        print(
            "WARNING: your key id does not start with 'rzp_test_'. "
            "Double-check you're using TEST MODE keys, not live keys, "
            "before continuing. Refusing to proceed with a non-test "
            "key as a safety precaution."
        )
        raise SystemExit(1)

    return (key_id, key_secret)


def create_payment_link(auth, tag: str) -> dict:
    """Create a real payment link on Razorpay's test-mode sandbox."""

    response = requests.post(
        f"{RAZORPAY_API_BASE}/payment_links",
        auth=auth,
        json={
            "amount": CAPTURE_AMOUNT_RUPEES * 100,  # paise
            "currency": "INR",
            "description": "Revive — one real captured test-mode failure",
            "notes": {"revive_capture_tag": tag},
        },
        timeout=15,
    )

    if response.status_code >= 300:
        raise SystemExit(
            f"Failed to create payment link "
            f"(HTTP {response.status_code}): {response.text}"
        )

    return response.json()


def find_matching_payment(auth, tag: str) -> dict | None:
    """
    List recent payments and find the one carrying our tag in notes.
    Matching client-side on notes, rather than relying on a specific
    query parameter, is the most robust approach regardless of exactly
    how Razorpay's list/filter API behaves at the time you run this.
    """

    response = requests.get(
        f"{RAZORPAY_API_BASE}/payments",
        auth=auth,
        params={"count": 20},
        timeout=15,
    )

    if response.status_code >= 300:
        print(f"  (payments list request failed: {response.status_code})")
        return None

    for payment in response.json().get("items", []):
        if payment.get("notes", {}).get("revive_capture_tag") == tag:
            return payment

    return None


def map_root_cause(payment: dict) -> tuple[str, bool]:
    """Returns (root_cause, was_confident_mapping)."""

    reason = (payment.get("error_reason") or "").strip().lower()

    if reason in ERROR_REASON_TO_ROOT_CAUSE:
        return ERROR_REASON_TO_ROOT_CAUSE[reason], True

    # Unmapped — default to the most generic "bank said no" bucket,
    # but flag it clearly rather than pretending we were sure.
    return "issuer_declined", False


def build_case(payment: dict, mapped_root_cause: str) -> dict:
    return {
        "case_id": "RV-REAL-00001",
        "surface": "subscription_failure",
        "customer_id": "cust_real_capture",
        "customer_name": "Real Razorpay Test-Mode Capture",
        "amount": round(payment.get("amount", 0) / 100, 2),
        "timestamp": payment.get("created_at_iso")
        or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root_cause_label": mapped_root_cause,
        "decline_code": mapped_root_cause,
        "bank": payment.get("bank") or payment.get("wallet") or "unknown",
        "card_network": payment.get("card", {}).get("network")
        if payment.get("card")
        else payment.get("method", "unknown"),
        "payment_method": payment.get("method", "unknown"),
        "customer_tenure_days": None,
        "retry_count": 0,
        # Full transparency: this is what makes this case verifiably
        # real rather than another synthetic row. Keep the raw error
        # fields alongside the mapped label.
        "source": "razorpay_test_mode_real",
        "razorpay_payment_id": payment.get("id"),
        "razorpay_raw_error": {
            "error_code": payment.get("error_code"),
            "error_description": payment.get("error_description"),
            "error_reason": payment.get("error_reason"),
            "error_source": payment.get("error_source"),
            "error_step": payment.get("error_step"),
        },
    }


def main() -> None:
    print("=" * 70)
    print("REVIVE — REAL RAZORPAY TEST-MODE CAPTURE")
    print("=" * 70)

    auth = _auth()
    tag = f"revive-{uuid.uuid4().hex[:12]}"

    print(f"\nCreating a real payment link on Razorpay's test-mode sandbox...")
    link = create_payment_link(auth, tag)
    short_url = link.get("short_url")

    if not short_url:
        raise SystemExit(f"No short_url in response: {link}")

    print(f"\n  Payment link created: {short_url}")
    print(f"  Link id:              {link.get('id')}")

    print(
        "\nOpening it in your browser now. To capture a REAL failure:\n"
        "\n  EASIEST — choose UPI as the payment method, enter the UPI ID:\n"
        "\n      failure@razorpay\n"
        "\n  This is Razorpay's own documented deterministic failure\n"
        "  trigger and will fail instantly and reliably.\n"
        "\n  ALTERNATIVE — choose Card, use one of Razorpay's published\n"
        "  test-failure card numbers (check razorpay.com/docs/payments/"
        "payments/test-card-details/\n  for the current list), and click "
        "'Failure' on the mock bank page.\n"
    )

    try:
        webbrowser.open(short_url)
    except Exception:
        print("  (couldn't auto-open a browser — open the link manually)")

    print(
        f"Waiting up to {POLL_TIMEOUT_SECONDS // 60} minutes for you to "
        f"complete it..."
    )

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    payment = None

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        payment = find_matching_payment(auth, tag)

        if payment is not None:
            break

        print("  ...still waiting")

    if payment is None:
        raise SystemExit(
            "\nTimed out without detecting a completed payment. "
            "Either the checkout wasn't finished, or notes weren't "
            "carried through as expected — in that case, run "
            "`GET /v1/payments` yourself, find the payment manually, "
            "and adapt build_case() to use it directly."
        )

    status = payment.get("status")
    print(f"\nFound a payment: id={payment.get('id')}, status={status}")

    if status != "failed":
        print(
            f"WARNING: this payment's status is '{status}', not "
            f"'failed'. If you accidentally completed a successful "
            f"payment, create a new link and try the failure@razorpay "
            f"UPI trick instead."
        )

    root_cause, confident = map_root_cause(payment)

    if not confident:
        print(
            f"\nNOTE: Razorpay's error_reason ('{payment.get('error_reason')}') "
            f"wasn't in our mapping table — defaulted to 'issuer_declined'. "
            f"Check ERROR_REASON_TO_ROOT_CAUSE in this file and add the "
            f"real value you observed if you want a more precise label."
        )

    case = build_case(payment, root_cause)

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(case, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Saved real captured case to: {OUTPUT_FILE}")
    print(f"  Real Razorpay error_code:        {payment.get('error_code')}")
    print(f"  Real Razorpay error_description: {payment.get('error_description')}")
    print(f"  Real Razorpay error_reason:      {payment.get('error_reason')}")
    print(f"  Mapped to Revive root cause:      {root_cause}")
    print(
        "\nThis file is intentionally kept separate from cases.json — "
        "see the module docstring for why. Reference it in your README "
        "or pitch as a real, verifiable Razorpay sandbox artifact."
    )


if __name__ == "__main__":
    main()
