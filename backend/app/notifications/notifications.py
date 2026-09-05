"""
Revive — Notifications & Alerts

Generates concise, human-readable notifications from the
completed Revive pipeline result.

This module does NOT implement recovery business logic.

It only reads already-computed pipeline results and identifies
events that are worth surfacing to a human operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ============================================================
# Notification
# ============================================================

@dataclass(frozen=True)
class Notification:
    """
    A single operator-facing notification.
    """

    notification_type: str
    severity: str
    title: str
    message: str
    case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_type": self.notification_type,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "case_id": self.case_id,
        }


# ============================================================
# Helpers
# ============================================================

def _value(
    item: dict[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]

    return default


def _money(value: Any) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


# ============================================================
# Notification generation
# ============================================================

def generate_notifications(
    cases: list[dict[str, Any]],
    psr_alerts: list[dict[str, Any]],
    a2a_settlements: list[dict[str, Any]],
    live_cases: list[dict[str, Any]] | None = None,
) -> list[Notification]:
    """
    Generate operator-facing notifications from an already
    completed Revive pipeline.

    Sources:

        psr_alerts
            Systemic payment-risk alerts.

        a2a_settlements
            Successful A2A recoveries, particularly cases that
            recovered despite the human-channel policy gate.

        cases
            High-value cases that were stopped and therefore
            deserve operator attention.

    This function does not modify cases, policy, ROI, A2A,
    or ledger state.
    """

    notifications: list[Notification] = []

    # ========================================================
    # PSR Guardian alerts
    # ========================================================

    for alert in psr_alerts:

        if not isinstance(alert, dict):
            continue

        # Real PSR Guardian alerts (see app/psr_guardian/guardian.py)
        # carry no "title" field, but they do carry the route
        # (bank/card_network) that triggered the anomaly — use that
        # to build a title that's actually useful to an operator
        # instead of a generic fallback.
        bank = alert.get("bank")
        card_network = alert.get("card_network")

        if bank and card_network:
            title = f"Payment Failure Cluster: {bank}/{card_network}"
        elif bank:
            title = f"Payment Failure Cluster: {bank}"
        else:
            title = _value(
                alert,
                "title",
                "alert_title",
                "type",
                "alert_type",
                default="PSR Guardian Alert",
            )

        message = _value(
            alert,
            "message",
            "description",
            "recommendation",
            "reason",
            default="PSR Guardian identified a systemic recovery risk.",
        )

        # Real alerts carry a real, deterministic severity
        # (CRITICAL / HIGH / MEDIUM / LOW — see
        # guardian.calculate_severity). Use it instead of hardcoding
        # "HIGH" for every alert, which would misreport a LOW
        # anomaly as urgent and a CRITICAL one as merely "HIGH".
        severity = str(
            _value(
                alert,
                "severity",
                default="HIGH",
            )
        ).strip().upper()

        if severity not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
            severity = "HIGH"

        notifications.append(
            Notification(
                notification_type="PSR_ALERT",
                severity=severity,
                title=str(title),
                message=str(message),
            )
        )

    # ========================================================
    # A2A settlements
    # ========================================================

    for settlement in a2a_settlements:

        if not isinstance(settlement, dict):
            continue

        outcome = str(
            _value(
                settlement,
                "outcome",
                "a2a_outcome",
                "status",
                "settlement_status",
                default="",
            )
        ).strip().upper()

        if outcome != "SETTLED":
            continue

        case_id = _value(
            settlement,
            "case_id",
            "id",
        )

        amount = _value(
            settlement,
            "amount",
            "final_amount",
            "settled_amount",
            "recovered_amount",
            default=0,
        )

        notifications.append(
            Notification(
                notification_type="A2A_RECOVERY",
                severity="SUCCESS",
                title="A2A Settlement Completed",
                message=(
                    f"Agent-to-agent settlement completed for "
                    f"{_money(amount)}."
                ),
                case_id=(
                    str(case_id)
                    if case_id is not None
                    else None
                ),
            )
        )

    # ========================================================
    # High-value stopped cases
    # ========================================================

    HIGH_VALUE_THRESHOLD = 100000.0

    for case in cases:

        if not isinstance(case, dict):
            continue

        outcome = str(
            _value(
                case,
                "outcome",
                default="",
            )
        ).strip().upper()

        if outcome != "NOT_RECOVERED":
            continue

        amount = _value(
            case,
            "amount",
            default=0,
        )

        try:
            amount_number = float(amount)
        except (TypeError, ValueError):
            continue

        if amount_number < HIGH_VALUE_THRESHOLD:
            continue

        case_id = _value(
            case,
            "case_id",
            default=None,
        )

        notifications.append(
            Notification(
                notification_type="HIGH_VALUE_STOPPED",
                severity="WARNING",
                title="High-Value Case Not Recovered",
                message=(
                    f"Case {_value(case, 'case_id', default='—')} "
                    f"ended without recovery despite having "
                    f"{_money(amount_number)} at stake."
                ),
                case_id=(
                    str(case_id)
                    if case_id is not None
                    else None
                ),
            )
        )

    # ========================================================
    # Live Razorpay payment failures (real, webhook-captured)
    # ========================================================

    for live_case in live_cases or []:

        if not isinstance(live_case, dict):
            continue

        case_id = _value(live_case, "case_id", default=None)
        amount = _value(live_case, "amount", default=0)
        root_cause = _value(
            live_case,
            "root_cause_label",
            "decline_code",
            default="unknown",
        )

        notifications.append(
            Notification(
                notification_type="LIVE_PAYMENT_FAILED",
                severity="HIGH",
                title="Real Payment Failure Captured",
                message=(
                    f"A live Razorpay payment of {_money(amount)} failed "
                    f"({root_cause}) and has entered the recovery pipeline."
                ),
                case_id=(
                    str(case_id)
                    if case_id is not None
                    else None
                ),
            )
        )
    return notifications


if __name__ == "__main__":
    import sys
    import os
    from dataclasses import asdict as _asdict

    sys.path.insert(
        0,
        os.path.join(os.path.dirname(__file__), "..", ".."),
    )

    from app.pipeline import RevivePipeline
    from app.diagnosis.classifier import load_cases

    print("=" * 72)
    print("REVIVE — NOTIFICATIONS & ALERTS")
    print("=" * 72)

    cases = load_cases()
    result = RevivePipeline().run(cases)

    case_dicts = [_asdict(c) for c in result.cases]
    psr_dicts = [_asdict(a) for a in result.psr_alerts]
    a2a_dicts = [_asdict(s) for s in result.a2a_results]

    items = generate_notifications(case_dicts, psr_dicts, a2a_dicts)

    by_severity: dict[str, int] = {}
    for n in items:
        by_severity[n.severity] = by_severity.get(n.severity, 0) + 1

    print(f"\nGenerated {len(items)} notifications.")
    print(f"By severity: {by_severity}")

    assert len(items) > 0, "expected at least one notification"

    assert any(
        n.notification_type == "PSR_ALERT" for n in items
    ), "expected at least one PSR_ALERT notification"

    assert any(
        n.notification_type == "A2A_RECOVERY" for n in items
    ), "expected at least one A2A_RECOVERY notification"

    for n in items:
        d = n.to_dict()
        assert d["title"], f"notification missing title: {n}"
        assert d["message"], f"notification missing message: {n}"

    print("\nSample notifications:\n")
    for n in items[:6]:
        print(f"  [{n.severity}] {n.title}")
        print(f"    {n.message}\n")

    print("=" * 72)
    print("NOTIFICATIONS SELF-TEST: PASSED")
    print("=" * 72)