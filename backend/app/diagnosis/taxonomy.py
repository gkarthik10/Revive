"""
Revive - Module 1
Root Cause Taxonomy

Single source of truth for all revenue-risk root causes.

IMPORTANT:
All downstream modules must import root causes from this file.
Do not redefine these labels elsewhere.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RootCause:
    """
    Represents a known revenue-loss root cause.
    """

    label: str
    typical_fix: str


ROOT_CAUSES = {
    "insufficient_funds": RootCause(
        label="insufficient_funds",
        typical_fix=(
            "Retry at a later time or send a payment reminder "
            "when funds are likely to be available."
        ),
    ),

    "otp_timeout": RootCause(
        label="otp_timeout",
        typical_fix=(
            "Prompt the customer to retry authentication "
            "and provide a short checkout recovery window."
        ),
    ),

    "issuer_declined": RootCause(
        label="issuer_declined",
        typical_fix=(
            "Retry through an eligible route or alternate "
            "payment method; investigate systemic issuer issues."
        ),
    ),

    "card_expired": RootCause(
        label="card_expired",
        typical_fix=(
            "Ask the customer to update their payment method "
            "before attempting another charge."
        ),
    ),

    "mandate_expired_or_revoked": RootCause(
        label="mandate_expired_or_revoked",
        typical_fix=(
            "Request mandate re-authorization before "
            "attempting another recurring payment."
        ),
    ),

    "mandate_debit_failed": RootCause(
        label="mandate_debit_failed",
        typical_fix=(
            "The mandate itself is still active; only this "
            "debit attempt failed (e.g. insufficient balance "
            "on the autopay date). Re-sequence the debit "
            "through the mandate retry sequencer, respecting "
            "the pre-debit notice window and per-cycle attempt "
            "cap, before escalating to re-authorization."
        ),
    ),

    "network_error": RootCause(
        label="network_error",
        typical_fix=(
            "Retry after a cooldown or route through "
            "an alternative payment path."
        ),
    ),

    "checkout_abandonment": RootCause(
        label="checkout_abandonment",
        typical_fix=(
            "Re-engage the customer with a direct checkout "
            "link and remove friction from the payment flow."
        ),
    ),

    "b2b_cashflow_delay": RootCause(
        label="b2b_cashflow_delay",
        typical_fix=(
            "Negotiate a bounded payment schedule or "
            "promise-to-pay date."
        ),
    ),

    "invoice_dispute": RootCause(
        label="invoice_dispute",
        typical_fix=(
            "Route to human finance support for dispute "
            "resolution before pursuing payment."
        ),
    ),

    "payment_approval_delay": RootCause(
        label="payment_approval_delay",
        typical_fix=(
            "Follow up with the customer's accounts or "
            "approval team."
        ),
    ),

    "administrative_delay": RootCause(
        label="administrative_delay",
        typical_fix=(
            "Send a structured payment reminder and "
            "request an expected settlement date."
        ),
    ),
}


def get_root_cause(label: str) -> RootCause:
    """
    Return a root cause definition by label.

    Raises:
        KeyError: if the label is unknown.
    """

    try:
        return ROOT_CAUSES[label]
    except KeyError as exc:
        raise KeyError(
            f"Unknown root cause: {label}"
        ) from exc


def is_valid_root_cause(label: str) -> bool:
    """
    Check whether a root cause exists in the taxonomy.
    """

    return label in ROOT_CAUSES


def all_root_causes() -> list[RootCause]:
    """
    Return all known root causes.
    """

    return list(ROOT_CAUSES.values())


if __name__ == "__main__":
    print("=" * 60)
    print("REVIVE — MODULE 1")
    print("Root Cause Taxonomy Self-Test")
    print("=" * 60)

    causes = all_root_causes()

    print(f"\nTotal root causes: {len(causes)}")

    for cause in causes:
        print(f"\n• {cause.label}")
        print(f"  Typical fix: {cause.typical_fix}")

    assert len(causes) == 12

    assert is_valid_root_cause("issuer_declined")
    assert is_valid_root_cause("b2b_cashflow_delay")
    assert not is_valid_root_cause("random_failure")

    assert (
        get_root_cause("issuer_declined").typical_fix
    )

    print()
    print("=" * 60)
    print("TAXONOMY SELF-TEST: PASSED")
    print("=" * 60)