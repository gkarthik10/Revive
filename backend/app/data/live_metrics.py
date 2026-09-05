from __future__ import annotations

from typing import Any


def build_live_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build operational Razorpay recovery metrics.

    IMPORTANT:
    These metrics are completely independent from the
    synthetic 105-case benchmark.

    Source of truth:
        live_case_store.load()
    """

    if not isinstance(cases, list):
        cases = []

    total_cases = len(cases)

    pending_cases = [
        case
        for case in cases
        if str(case.get("recovery_status", "")).upper()
        == "PENDING_RECOVERY"
    ]

    recovered_cases = [
        case
        for case in cases
        if str(case.get("recovery_status", "")).upper()
        == "RECOVERED"
    ]

    total_amount = sum(
        float(case.get("amount") or 0)
        for case in cases
    )

    pending_amount = sum(
        float(case.get("amount") or 0)
        for case in pending_cases
    )

    recovered_amount = sum(
        float(case.get("recovered_amount") or 0)
        for case in recovered_cases
    )

    retry_links = sum(
        int(case.get("retry_count") or 0)
        for case in cases
    )

    cases_with_retry = sum(
        1
        for case in cases
        if int(case.get("retry_count") or 0) > 0
    )

    recovery_rate = (
        recovered_amount / total_amount
        if total_amount > 0
        else 0.0
    )

    # --------------------------------------------------------
    # Operational funnel
    # --------------------------------------------------------

    payment_failed = total_cases

    recovery_pending = len(pending_cases)

    retry_issued = cases_with_retry

    payment_captured = len(recovered_cases)

    recovery_completed = len(
        [
            case
            for case in recovered_cases
            if case.get("recovery_source")
            == "razorpay_payment_captured"
        ]
    )

    return {
        "live_cases": total_cases,
        "pending_recovery": len(pending_cases),
        "recovered_cases": len(recovered_cases),

        "total_amount": round(total_amount, 2),
        "pending_amount": round(pending_amount, 2),
        "recovered_amount": round(recovered_amount, 2),

        "live_recovery_rate": round(recovery_rate, 6),

        "retry_links": retry_links,
        "cases_with_retry": cases_with_retry,

        "funnel": {
            "payment_failed": payment_failed,
            "recovery_pending": recovery_pending,
            "retry_issued": retry_issued,
            "payment_captured": payment_captured,
            "recovery_completed": recovery_completed,
        },

        "source": "razorpay_live_case_store",
        "is_live": True,
    }