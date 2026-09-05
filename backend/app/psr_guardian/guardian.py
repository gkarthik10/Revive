"""
Revive - Module 2
PSR Guardian

Payment Systemic Risk Guardian detects systemic payment-route
anomalies across the transaction batch.

Core idea:

    Individual payment failures
                ↓
        Group by payment route
                ↓
        Analyze time concentration
                ↓
        Detect abnormal clustering
                ↓
          RouteAlert
                ↓
       Recommend intervention

The module deliberately does NOT use the synthetic
root_cause_label field for detection.

It uses only operationally observable fields:

    bank
    card_network
    decline_code
    timestamp

The root_cause_label is used only by the self-test to verify
that the detected anomaly corresponds to the planted dataset
scenario.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict


# ============================================================
# Configuration
# ============================================================

CURRENT_DIR = Path(__file__).resolve().parent

DATA_FILE = (
    CURRENT_DIR.parent
    / "data"
    / "cases.json"
)


# Only these failures are useful for detecting payment-route
# systemic problems.
ROUTE_ATTRIBUTABLE_CODES = {
    "issuer_declined",
    "network_error",
}


# A group must contain at least this many failures before
# considering it a systemic candidate.
MIN_GROUP_SIZE = 5


# A sufficiently concentrated group should be considered
# suspicious.
CONCENTRATION_THRESHOLD = 0.60


# Time window used to evaluate concentration.
WINDOW_MINUTES = 120


# ============================================================
# Data classes
# ============================================================

@dataclass(frozen=True)
class RouteAlert:
    """
    Represents a detected systemic payment-route anomaly.
    """

    bank: str
    card_network: str
    decline_code: str

    group_size: int

    window_start: str
    window_end: str

    concentrated_cases: int
    concentration_ratio: float

    severity: str
    recommendation: str

    evidence: list[str]


# ============================================================
# Data loading
# ============================================================

def load_cases() -> list[dict]:
    """
    Load the deterministic synthetic dataset.
    """

    with DATA_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


# ============================================================
# Filtering
# ============================================================

def filter_route_attributable_cases(
    cases: list[dict],
) -> list[dict]:
    """
    Keep only subscription failures whose failure code can
    reasonably indicate a payment-route/systemic problem.

    IMPORTANT:
    root_cause_label is intentionally NOT used.
    """

    return [
        case
        for case in cases
        if (
            case.get("surface")
            == "subscription_failure"
            and case.get("decline_code")
            in ROUTE_ATTRIBUTABLE_CODES
        )
    ]


# ============================================================
# Grouping
# ============================================================

def group_by_route(
    cases: list[dict],
) -> dict[tuple[str, str, str], list[dict]]:
    """
    Group route-attributable failures by:

        bank
        card network
        decline code
    """

    groups = defaultdict(list)

    for case in cases:

        key = (
            case["bank"],
            case["card_network"],
            case["decline_code"],
        )

        groups[key].append(case)

    return dict(groups)


# ============================================================
# Temporal concentration
# ============================================================

def find_densest_window(
    cases: list[dict],
    window_minutes: int = WINDOW_MINUTES,
) -> tuple[list[dict], datetime, datetime]:
    """
    Find the densest time window containing the largest number
    of failures.

    Sliding-window implementation.

    Returns:

        concentrated cases
        window start
        window end
    """

    if not cases:
        raise ValueError(
            "Cannot calculate temporal concentration "
            "for an empty case list."
        )

    sorted_cases = sorted(
        cases,
        key=lambda case: datetime.fromisoformat(
            case["timestamp"]
        ),
    )

    best_cases = []
    best_start = None
    best_end = None

    left = 0

    for right in range(len(sorted_cases)):

        right_time = datetime.fromisoformat(
            sorted_cases[right]["timestamp"]
        )

        while left <= right:

            left_time = datetime.fromisoformat(
                sorted_cases[left]["timestamp"]
            )

            if (
                right_time - left_time
            ).total_seconds() <= window_minutes * 60:

                break

            left += 1

        current_cases = sorted_cases[
            left : right + 1
        ]

        if len(current_cases) > len(best_cases):

            best_cases = list(current_cases)

            best_start = datetime.fromisoformat(
                current_cases[0]["timestamp"]
            )

            best_end = (
                best_start
                + timedelta(
                    minutes=window_minutes
                )
            )

    return (
        best_cases,
        best_start,
        best_end,
    )


# ============================================================
# Severity
# ============================================================

def calculate_severity(
    concentration_ratio: float,
    group_size: int,
) -> str:
    """
    Convert anomaly strength into a human-readable severity.

    This is deliberately deterministic.
    """

    if (
        concentration_ratio >= 0.80
        and group_size >= 10
    ):
        return "CRITICAL"

    if (
        concentration_ratio >= 0.60
        and group_size >= 5
    ):
        return "HIGH"

    if concentration_ratio >= 0.50:
        return "MEDIUM"

    return "LOW"


# ============================================================
# Recommendation
# ============================================================

def generate_recommendation(
    bank: str,
    card_network: str,
    decline_code: str,
    severity: str,
) -> str:
    """
    Generate an operational recommendation.

    This does not execute the recommendation.
    """

    if decline_code == "issuer_declined":

        return (
            f"Investigate the {bank}/{card_network} route, "
            "consider rerouting eligible traffic, and avoid "
            "blindly retrying affected transactions until "
            "route health is confirmed."
        )

    if decline_code == "network_error":

        return (
            f"Investigate network health for the "
            f"{bank}/{card_network} route and route eligible "
            "traffic through an alternative path."
        )

    return (
        "Investigate the affected payment route before "
        "continuing automated recovery."
    )


# ============================================================
# Detect anomaly
# ============================================================

def detect_alerts(
    cases: list[dict],
) -> list[RouteAlert]:
    """
    Detect systemic payment-route anomalies.

    Detection criteria:

        group_size >= 5

        concentration_ratio >= 0.60

    concentration_ratio:

        failures inside densest 2-hour window
        -------------------------------------
                    total group failures
    """

    route_cases = filter_route_attributable_cases(
        cases
    )

    groups = group_by_route(
        route_cases
    )

    alerts = []

    for (
        bank,
        card_network,
        decline_code,
    ), group_cases in groups.items():

        group_size = len(group_cases)

        if group_size < MIN_GROUP_SIZE:
            continue

        (
            concentrated_cases,
            window_start,
            window_end,
        ) = find_densest_window(
            group_cases
        )

        concentrated_count = len(
            concentrated_cases
        )

        concentration_ratio = (
            concentrated_count / group_size
        )

        if (
            concentration_ratio
            < CONCENTRATION_THRESHOLD
        ):
            continue

        severity = calculate_severity(
            concentration_ratio,
            group_size,
        )

        recommendation = generate_recommendation(
            bank,
            card_network,
            decline_code,
            severity,
        )

        evidence = [
            (
                f"Route group contains "
                f"{group_size} failures."
            ),
            (
                f"{concentrated_count} failures occur "
                f"within a {WINDOW_MINUTES}-minute "
                "window."
            ),
            (
                f"Temporal concentration ratio = "
                f"{concentration_ratio:.2%}."
            ),
            (
                f"Route = {bank}/{card_network}."
            ),
            (
                f"Failure code = {decline_code}."
            ),
        ]

        alerts.append(
            RouteAlert(
                bank=bank,
                card_network=card_network,
                decline_code=decline_code,
                group_size=group_size,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                concentrated_cases=concentrated_count,
                concentration_ratio=concentration_ratio,
                severity=severity,
                recommendation=recommendation,
                evidence=evidence,
            )
        )

    # Strongest alerts first.
    alerts.sort(
        key=lambda alert: (
            alert.severity == "CRITICAL",
            alert.severity == "HIGH",
            alert.concentration_ratio,
            alert.group_size,
        ),
        reverse=True,
    )

    return alerts


# ============================================================
# Self-test
# ============================================================

def main() -> None:

    print("=" * 70)
    print("REVIVE — MODULE 2")
    print("PSR Guardian — Payment Systemic Risk Detection")
    print("=" * 70)

    cases = load_cases()

    print()
    print(f"Loaded cases: {len(cases)}")

    route_cases = filter_route_attributable_cases(
        cases
    )

    print(
        f"Route-attributable cases: "
        f"{len(route_cases)}"
    )

    alerts = detect_alerts(cases)

    print(
        f"Systemic alerts detected: "
        f"{len(alerts)}"
    )

    print()

    if alerts:

        print("Detected alerts:")

        for index, alert in enumerate(
            alerts,
            start=1,
        ):

            print()
            print(
                f"Alert #{index}"
            )

            print(
                f"  Route:              "
                f"{alert.bank}/{alert.card_network}"
            )

            print(
                f"  Failure code:       "
                f"{alert.decline_code}"
            )

            print(
                f"  Group size:         "
                f"{alert.group_size}"
            )

            print(
                f"  Concentrated:       "
                f"{alert.concentrated_cases}"
            )

            print(
                f"  Concentration:      "
                f"{alert.concentration_ratio:.2%}"
            )

            print(
                f"  Severity:           "
                f"{alert.severity}"
            )

            print(
                f"  Window:             "
                f"{alert.window_start}"
                f" → "
                f"{alert.window_end}"
            )

            print(
                f"  Recommendation:     "
                f"{alert.recommendation}"
            )

            print("  Evidence:")

            for evidence in alert.evidence:
                print(
                    f"    - {evidence}"
                )

    # --------------------------------------------------------
    # Validate planted anomaly
    # --------------------------------------------------------

    planted_alerts = [
        alert
        for alert in alerts
        if (
            alert.bank == "HDFC"
            and alert.card_network == "VISA"
            and alert.decline_code
            == "issuer_declined"
        )
    ]

    assert len(planted_alerts) >= 1, (
        "PSR Guardian failed to detect the planted "
        "HDFC/VISA issuer_declined anomaly."
    )

    planted = planted_alerts[0]

    assert planted.group_size >= 14, (
        "Expected the planted route group to contain "
        "at least 14 failures."
    )

    assert (
        planted.concentration_ratio
        >= CONCENTRATION_THRESHOLD
    ), (
        "Detected anomaly does not meet the required "
        "concentration threshold."
    )

    assert planted.concentrated_cases >= 10, (
        "Expected strong temporal concentration in "
        "the planted anomaly."
    )

    assert planted.severity in {
        "HIGH",
        "CRITICAL",
    }

    assert planted.recommendation

    assert planted.evidence

    # --------------------------------------------------------
    # Ensure detection did not rely on ground truth.
    # --------------------------------------------------------

    print()
    print(
        "Detection grounding:"
    )

    print(
        "  Used operational fields only:"
    )

    print(
        "    ✓ bank"
    )

    print(
        "    ✓ card_network"
    )

    print(
        "    ✓ decline_code"
    )

    print(
        "    ✓ timestamp"
    )

    print(
        "    ✗ root_cause_label"
    )

    print()
    print("=" * 70)
    print("MODULE 2 SELF-TEST: PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()