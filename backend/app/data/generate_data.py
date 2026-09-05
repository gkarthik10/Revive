"""
Revive - Module 0
Synthetic Revenue Recovery Dataset Generator

Generates a deterministic synthetic dataset for the Revive AI Revenue
Recovery system.

Dataset:
    45 subscription failures
    35 checkout abandonments
    25 B2B receivables
    -------------------------
    105 total cases

Random seed:
    42

A deliberate systemic anomaly is planted:
    HDFC + VISA + issuer_declined
    14 cases concentrated within a short time window.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import datetime, timedelta
from pathlib import Path


# ============================================================
# Configuration
# ============================================================

SEED = 42

TOTAL_SUBSCRIPTION_FAILURES = 45
TOTAL_CHECKOUT_ABANDONMENTS = 35
TOTAL_B2B_RECEIVABLES = 25

OUTPUT_DIR = Path(__file__).resolve().parent

JSON_FILE = OUTPUT_DIR / "cases.json"
CSV_FILE = OUTPUT_DIR / "cases.csv"

BASE_TIMESTAMP = datetime(2026, 8, 25, 9, 0, 0)


# ============================================================
# Shared synthetic data
# ============================================================

CUSTOMERS = [
    ("CUST-001", "Aarav"),
    ("CUST-002", "Priya"),
    ("CUST-003", "Rahul"),
    ("CUST-004", "Ananya"),
    ("CUST-005", "Vikram"),
    ("CUST-006", "Sneha"),
    ("CUST-007", "Arjun"),
    ("CUST-008", "Meera"),
    ("CUST-009", "Rohan"),
    ("CUST-010", "Kavya"),
    ("CUST-011", "Aditya"),
    ("CUST-012", "Ishita"),
    ("CUST-013", "Karan"),
    ("CUST-014", "Nisha"),
    ("CUST-015", "Sanjay"),
    ("CUST-016", "Divya"),
    ("CUST-017", "Manish"),
    ("CUST-018", "Pooja"),
    ("CUST-019", "Varun"),
    ("CUST-020", "Neha"),
]


BANKS = [
    "HDFC",
    "ICICI",
    "SBI",
    "AXIS",
    "KOTAK",
]


CARD_NETWORKS = [
    "VISA",
    "MASTERCARD",
    "RUPAY",
]


PAYMENT_METHODS = [
    "upi",
    "credit_card",
    "debit_card",
]


DEVICES = [
    "mobile",
    "desktop",
    "tablet",
]


SUBSCRIPTION_ROOT_CAUSES = [
    "insufficient_funds",
    "otp_timeout",
    "issuer_declined",
    "card_expired",
    "mandate_expired_or_revoked",
    "mandate_debit_failed",
    "network_error",
]


# ============================================================
# Utility functions
# ============================================================

def random_customer(rng: random.Random) -> tuple[str, str]:
    return rng.choice(CUSTOMERS)


def random_amount(
    rng: random.Random,
    minimum: int = 500,
    maximum: int = 25000,
) -> int:
    return rng.randint(minimum, maximum)


def iso_timestamp(timestamp: datetime) -> str:
    return timestamp.isoformat()


def create_base_case(
    case_id: str,
    surface: str,
    customer_id: str,
    customer_name: str,
    amount: int,
    timestamp: datetime,
    root_cause_label: str,
) -> dict:
    return {
        "case_id": case_id,
        "surface": surface,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "amount": amount,
        "timestamp": iso_timestamp(timestamp),
        "root_cause_label": root_cause_label,
    }


# ============================================================
# Subscription failures
# ============================================================

def generate_subscription_failures(
    rng: random.Random,
) -> list[dict]:
    cases = []

    # --------------------------------------------------------
    # Deliberately planted systemic anomaly
    #
    # 14 issuer_declined cases:
    # HDFC + VISA
    # concentrated in a 90-minute window.
    # --------------------------------------------------------

    anomaly_start = BASE_TIMESTAMP + timedelta(hours=20)

    for index in range(14):
        customer_id, customer_name = random_customer(rng)

        timestamp = anomaly_start + timedelta(
            minutes=rng.randint(0, 89)
        )

        case = create_base_case(
            case_id=f"RV-{index + 1:05d}",
            surface="subscription_failure",
            customer_id=customer_id,
            customer_name=customer_name,
            amount=random_amount(rng, 1000, 20000),
            timestamp=timestamp,
            root_cause_label="issuer_declined",
        )

        case.update(
            {
                "decline_code": "issuer_declined",
                "bank": "HDFC",
                "card_network": "VISA",
                "payment_method": "credit_card",
                "customer_tenure_days": rng.randint(30, 900),
                "retry_count": rng.randint(0, 2),
            }
        )

        cases.append(case)

    # --------------------------------------------------------
    # Remaining subscription failures
    # --------------------------------------------------------

    remaining = TOTAL_SUBSCRIPTION_FAILURES - len(cases)

    for index in range(remaining):
        customer_id, customer_name = random_customer(rng)

        root_cause = rng.choice(SUBSCRIPTION_ROOT_CAUSES)

        # Avoid accidentally creating another large anomaly.
        bank = rng.choice(BANKS)
        network = rng.choice(CARD_NETWORKS)

        timestamp = BASE_TIMESTAMP + timedelta(
            hours=rng.randint(0, 72),
            minutes=rng.randint(0, 59),
        )

        case_number = len(cases) + 1

        case = create_base_case(
            case_id=f"RV-{case_number:05d}",
            surface="subscription_failure",
            customer_id=customer_id,
            customer_name=customer_name,
            amount=random_amount(rng, 500, 25000),
            timestamp=timestamp,
            root_cause_label=root_cause,
        )

        case.update(
            {
                "decline_code": root_cause,
                "bank": bank,
                "card_network": network,
                "payment_method": rng.choice(PAYMENT_METHODS),
                "customer_tenure_days": rng.randint(15, 1200),
                "retry_count": rng.randint(0, 3),
            }
        )

        cases.append(case)

    return cases


# ============================================================
# Checkout abandonment
# ============================================================

def generate_checkout_abandonments(
    rng: random.Random,
    starting_case_number: int,
) -> list[dict]:
    cases = []

    for index in range(TOTAL_CHECKOUT_ABANDONMENTS):
        customer_id, customer_name = random_customer(rng)

        payment_attempted = rng.choice([True, False])

        if payment_attempted:
            root_cause = rng.choice(
                [
                    "otp_timeout",
                    "network_error",
                    "issuer_declined",
                ]
            )
        else:
            root_cause = "checkout_abandonment"

        timestamp = BASE_TIMESTAMP + timedelta(
            hours=rng.randint(0, 72),
            minutes=rng.randint(0, 59),
        )

        case_number = starting_case_number + index

        case = create_base_case(
            case_id=f"RV-{case_number:05d}",
            surface="checkout_abandonment",
            customer_id=customer_id,
            customer_name=customer_name,
            amount=random_amount(rng, 800, 30000),
            timestamp=timestamp,
            root_cause_label=root_cause,
        )

        case.update(
            {
                "payment_attempted": payment_attempted,
                "time_to_abandon_seconds": rng.randint(
                    15,
                    900,
                ),
                "device": rng.choice(DEVICES),
                "payment_method_selected": rng.choice(
                    PAYMENT_METHODS
                ),
            }
        )

        cases.append(case)

    return cases


# ============================================================
# B2B receivables
# ============================================================

def generate_b2b_receivables(
    rng: random.Random,
    starting_case_number: int,
) -> list[dict]:
    cases = []

    b2b_root_causes = [
        "b2b_cashflow_delay",
        "invoice_dispute",
        "payment_approval_delay",
        "administrative_delay",
    ]

    notes_by_cause = {
        "b2b_cashflow_delay": [
            "Our cash flow is tight this month.",
            "We need additional time before settling the invoice.",
            "Payment is delayed due to internal cash flow constraints.",
        ],
        "invoice_dispute": [
            "There is a dispute regarding the invoice amount.",
            "Our finance team has raised an invoice discrepancy.",
            "Please resolve the invoice issue before payment.",
        ],
        "payment_approval_delay": [
            "The payment is waiting for internal approval.",
            "Our finance approval process is taking longer than expected.",
            "The invoice is pending management approval.",
        ],
        "administrative_delay": [
            "Payment is delayed due to an administrative issue.",
            "Our accounts team is processing the invoice.",
            "There is a delay in our internal payment workflow.",
        ],
    }

    for index in range(TOTAL_B2B_RECEIVABLES):
        customer_id, customer_name = random_customer(rng)

        root_cause = rng.choice(b2b_root_causes)

        due_date = (
            BASE_TIMESTAMP.date()
            - timedelta(days=rng.randint(1, 45))
        )

        days_overdue = (
            BASE_TIMESTAMP.date() - due_date
        ).days

        timestamp = BASE_TIMESTAMP + timedelta(
            hours=rng.randint(0, 72),
            minutes=rng.randint(0, 59),
        )

        case_number = starting_case_number + index

        case = create_base_case(
            case_id=f"RV-{case_number:05d}",
            surface="b2b_receivable",
            customer_id=customer_id,
            customer_name=customer_name,
            amount=random_amount(rng, 10000, 250000),
            timestamp=timestamp,
            root_cause_label=root_cause,
        )

        case.update(
            {
                "due_date": due_date.isoformat(),
                "days_overdue": days_overdue,
                "has_ap_agent": rng.choice([True, False]),
                "comment_notes": rng.choice(
                    notes_by_cause[root_cause]
                ),
            }
        )

        cases.append(case)

    return cases


# ============================================================
# Generate complete dataset
# ============================================================

def generate_dataset(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)

    subscription_cases = generate_subscription_failures(rng)

    checkout_cases = generate_checkout_abandonments(
        rng,
        starting_case_number=len(subscription_cases) + 1,
    )

    b2b_cases = generate_b2b_receivables(
        rng,
        starting_case_number=(
            len(subscription_cases)
            + len(checkout_cases)
            + 1
        ),
    )

    cases = (
        subscription_cases
        + checkout_cases
        + b2b_cases
    )

    return cases


# ============================================================
# Validation
# ============================================================

def validate_dataset(cases: list[dict]) -> None:
    assert len(cases) == 105, (
        f"Expected 105 cases, got {len(cases)}"
    )

    surfaces = {}

    for case in cases:
        surface = case["surface"]
        surfaces[surface] = surfaces.get(surface, 0) + 1

    assert surfaces["subscription_failure"] == 45
    assert surfaces["checkout_abandonment"] == 35
    assert surfaces["b2b_receivable"] == 25

    # Verify unique case IDs.
    case_ids = [case["case_id"] for case in cases]

    assert len(case_ids) == len(set(case_ids)), (
        "Duplicate case IDs detected."
    )

    # Verify planted anomaly.
    anomaly_cases = [
        case
        for case in cases
        if (
            case["surface"] == "subscription_failure"
            and case["bank"] == "HDFC"
            and case["card_network"] == "VISA"
            and case["decline_code"] == "issuer_declined"
        )
    ]

    assert len(anomaly_cases) >= 14, (
        "Planted HDFC/VISA issuer_declined anomaly "
        "was not generated correctly."
    )


# ============================================================
# Output
# ============================================================

def save_json(cases: list[dict]) -> None:
    with JSON_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            cases,
            file,
            indent=2,
            ensure_ascii=False,
        )


def save_csv(cases: list[dict]) -> None:
    fieldnames = sorted(
        {
            key
            for case in cases
            for key in case.keys()
        }
    )

    with CSV_FILE.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for case in cases:
            writer.writerow(case)


# ============================================================
# Self-test
# ============================================================

def main() -> None:
    print("=" * 60)
    print("REVIVE — MODULE 0")
    print("Synthetic Revenue Recovery Dataset Generator")
    print("=" * 60)

    cases = generate_dataset(SEED)

    validate_dataset(cases)

    save_json(cases)
    save_csv(cases)

    print()
    print("Dataset generated successfully.")
    print()

    print(f"Random seed: {SEED}")
    print(f"Total cases: {len(cases)}")
    print()

    print("Surface distribution:")

    surface_counts = {}

    for case in cases:
        surface = case["surface"]
        surface_counts[surface] = (
            surface_counts.get(surface, 0) + 1
        )

    for surface, count in surface_counts.items():
        print(f"  {surface:<25} {count}")

    print()

    anomaly_cases = [
        case
        for case in cases
        if (
            case["surface"] == "subscription_failure"
            and case["bank"] == "HDFC"
            and case["card_network"] == "VISA"
            and case["decline_code"] == "issuer_declined"
        )
    ]

    print("Planted systemic anomaly:")
    print("  Bank:          HDFC")
    print("  Network:       VISA")
    print("  Failure:       issuer_declined")
    print(f"  Matching cases: {len(anomaly_cases)}")

    print()

    print("Output files:")
    print(f"  JSON: {JSON_FILE}")
    print(f"  CSV:  {CSV_FILE}")

    print()
    print("First 3 cases:")

    for case in cases[:3]:
        print()
        print(json.dumps(case, indent=2))

    print()
    print("=" * 60)
    print("MODULE 0 SELF-TEST: PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()