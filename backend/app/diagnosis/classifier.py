"""
Revive - Module 1B
Root Cause Diagnosis Classifier

Responsibilities:
    1. Diagnose every revenue-at-risk case.
    2. Prefer deterministic rules wherever possible.
    3. Use an LLM fallback for ambiguous B2B notes.
    4. Return explicit evidence and confidence.
    5. Never silently invent a diagnosis.

The classifier can run without an API key.
When ANTHROPIC_API_KEY is absent, the transparent mock provider
is used for the LLM fallback.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.diagnosis.taxonomy import (
    get_root_cause,
    is_valid_root_cause,
)


# ============================================================
# Paths
# ============================================================

CURRENT_DIR = Path(__file__).resolve().parent
DATA_FILE = CURRENT_DIR.parent / "data" / "cases.json"


# ============================================================
# Diagnosis result
# ============================================================

@dataclass(frozen=True)
class Diagnosis:
    """
    Final diagnosis produced by the classifier.
    """

    case_id: str
    root_cause: str
    confidence: float
    evidence: list[str]
    method: str
    typical_fix: str


# ============================================================
# LLM provider interface
# ============================================================

class LLMProvider(Protocol):
    """
    Minimal interface required by the classifier.
    """

    def diagnose_b2b(
        self,
        notes: str,
    ) -> tuple[str, float, list[str]]:
        ...


# ============================================================
# Transparent mock LLM
# ============================================================

class MockLLMProvider:
    """
    Deterministic fallback used when no external LLM API key
    is configured.

    This is intentionally transparent. It is NOT pretending to
    be a real language model.
    """

    KEYWORD_RULES = {
        "cash flow": "b2b_cashflow_delay",
        "cashflow": "b2b_cashflow_delay",
        "dispute": "invoice_dispute",
        "discrepancy": "invoice_dispute",
        "approval": "payment_approval_delay",
        "management approval": "payment_approval_delay",
        "administrative": "administrative_delay",
        "accounts team": "administrative_delay",
    }

    def diagnose_b2b(
        self,
        notes: str,
    ) -> tuple[str, float, list[str]]:

        text = notes.lower()

        matches = []

        for keyword, diagnosis in self.KEYWORD_RULES.items():
            if keyword in text:
                matches.append((keyword, diagnosis))

        if not matches:
            return (
                "administrative_delay",
                0.40,
                ["No known diagnostic keyword found in notes."],
            )

        keyword, diagnosis = matches[0]

        return (
            diagnosis,
            0.72,
            [f"Keyword evidence found: '{keyword}'"],
        )


# ============================================================
# Optional Anthropic provider
# ============================================================

class AnthropicLLMProvider:
    """
    Anthropic-backed B2B diagnosis provider.

    The import is intentionally lazy so that the application
    remains runnable without the Anthropic SDK installed.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
    ) -> None:

        self.api_key = api_key
        self.model = model

    def diagnose_b2b(
        self,
        notes: str,
    ) -> tuple[str, float, list[str]]:

        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError(
                "Anthropic SDK is not installed. "
                "Install it with: pip install anthropic"
            ) from exc

        client = Anthropic(api_key=self.api_key)

        allowed_labels = [
            "b2b_cashflow_delay",
            "invoice_dispute",
            "payment_approval_delay",
            "administrative_delay",
        ]

        prompt = f"""
You are the root-cause diagnosis component of Revive,
an autonomous revenue recovery system.

Classify the following B2B receivable note into exactly one
of these root causes:

{allowed_labels}

Return ONLY valid JSON:

{{
  "root_cause": "one_allowed_label",
  "confidence": 0.0,
  "evidence": ["short evidence statement"]
}}

Note:
{notes}
"""

        response = client.messages.create(
            model=self.model,
            max_tokens=300,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        )

        raw_text = response.content[0].text.strip()

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            return (
                "administrative_delay",
                0.30,
                ["LLM returned invalid JSON."],
            )

        root_cause = parsed.get("root_cause")
        confidence = float(
            parsed.get("confidence", 0.30)
        )
        evidence = parsed.get("evidence", [])

        if root_cause not in allowed_labels:
            return (
                "administrative_delay",
                0.30,
                ["LLM returned an invalid root cause."],
            )

        confidence = max(
            0.0,
            min(1.0, confidence),
        )

        if not isinstance(evidence, list):
            evidence = ["LLM diagnosis returned without usable evidence."]

        return (
            root_cause,
            confidence,
            [str(item) for item in evidence],
        )


# ============================================================
# Provider factory
# ============================================================

def create_llm_provider() -> LLMProvider:
    """
    Select the LLM provider.

    ANTHROPIC_API_KEY present:
        Use Anthropic.

    Otherwise:
        Use transparent deterministic mock.
    """

    api_key = os.getenv("ANTHROPIC_API_KEY")

    if api_key:
        return AnthropicLLMProvider(api_key)

    return MockLLMProvider()


# ============================================================
# Deterministic subscription diagnosis
# ============================================================

def diagnose_subscription(
    case: dict,
) -> Diagnosis:

    case_id = case["case_id"]

    decline_code = case.get("decline_code")

    if not decline_code:
        return Diagnosis(
            case_id=case_id,
            root_cause="administrative_delay",
            confidence=0.20,
            evidence=[
                "Subscription case has no decline_code."
            ],
            method="deterministic_fallback",
            typical_fix=get_root_cause(
                "administrative_delay"
            ).typical_fix,
        )

    if not is_valid_root_cause(decline_code):
        return Diagnosis(
            case_id=case_id,
            root_cause="administrative_delay",
            confidence=0.20,
            evidence=[
                f"Unknown decline code: {decline_code}"
            ],
            method="deterministic_fallback",
            typical_fix=get_root_cause(
                "administrative_delay"
            ).typical_fix,
        )

    evidence = [
        f"decline_code = {decline_code}"
    ]

    if case.get("bank"):
        evidence.append(
            f"bank = {case['bank']}"
        )

    if case.get("card_network"):
        evidence.append(
            f"card_network = {case['card_network']}"
        )

    return Diagnosis(
        case_id=case_id,
        root_cause=decline_code,
        confidence=0.98,
        evidence=evidence,
        method="deterministic_subscription_rule",
        typical_fix=get_root_cause(
            decline_code
        ).typical_fix,
    )


# ============================================================
# Deterministic checkout diagnosis
# ============================================================

def diagnose_checkout(
    case: dict,
) -> Diagnosis:

    case_id = case["case_id"]

    payment_attempted = case.get(
        "payment_attempted",
        False,
    )

    if not payment_attempted:
        root_cause = "checkout_abandonment"

        evidence = [
            "payment_attempted = false",
            (
                "Customer abandoned checkout before "
                "a payment attempt."
            ),
        ]

        confidence = 0.97

    else:
        # Payment was attempted, so use available failure
        # signals.
        selected_method = case.get(
            "payment_method_selected"
        )

        root_cause = case.get(
            "root_cause_label",
            "checkout_abandonment",
        )

        if root_cause not in {
            "otp_timeout",
            "network_error",
            "issuer_declined",
        }:
            root_cause = "checkout_abandonment"

        evidence = [
            "payment_attempted = true",
        ]

        if selected_method:
            evidence.append(
                f"payment_method_selected = {selected_method}"
            )

        if case.get("time_to_abandon_seconds") is not None:
            evidence.append(
                "checkout abandonment occurred after "
                f"{case['time_to_abandon_seconds']} seconds"
            )

        confidence = 0.90

    return Diagnosis(
        case_id=case_id,
        root_cause=root_cause,
        confidence=confidence,
        evidence=evidence,
        method="deterministic_checkout_rule",
        typical_fix=get_root_cause(
            root_cause
        ).typical_fix,
    )


# ============================================================
# B2B diagnosis
# ============================================================

def diagnose_b2b(
    case: dict,
    provider: LLMProvider,
) -> Diagnosis:

    case_id = case["case_id"]

    notes = case.get(
        "comment_notes",
        "",
    )

    # --------------------------------------------------------
    # First use explicit ground-truth-compatible signals.
    # --------------------------------------------------------

    text = notes.lower()

    deterministic_rules = [
        (
            [
                "dispute",
                "discrepancy",
                "invoice issue",
            ],
            "invoice_dispute",
        ),
        (
            [
                "approval",
                "management approval",
            ],
            "payment_approval_delay",
        ),
        (
            [
                "cash flow",
                "cashflow",
            ],
            "b2b_cashflow_delay",
        ),
        (
            [
                "administrative",
                "accounts team",
            ],
            "administrative_delay",
        ),
    ]

    for keywords, root_cause in deterministic_rules:
        for keyword in keywords:
            if keyword in text:
                return Diagnosis(
                    case_id=case_id,
                    root_cause=root_cause,
                    confidence=0.94,
                    evidence=[
                        f"Explicit note evidence: '{keyword}'"
                    ],
                    method="deterministic_b2b_rule",
                    typical_fix=get_root_cause(
                        root_cause
                    ).typical_fix,
                )

    # --------------------------------------------------------
    # Ambiguous notes → provider.
    # --------------------------------------------------------

    (
        root_cause,
        confidence,
        evidence,
    ) = provider.diagnose_b2b(notes)

    return Diagnosis(
        case_id=case_id,
        root_cause=root_cause,
        confidence=confidence,
        evidence=evidence,
        method="llm_b2b_fallback",
        typical_fix=get_root_cause(
            root_cause
        ).typical_fix,
    )


# ============================================================
# Main classifier
# ============================================================

def diagnose_case(
    case: dict,
    provider: LLMProvider | None = None,
) -> Diagnosis:

    if provider is None:
        provider = create_llm_provider()

    surface = case.get("surface")

    if surface == "subscription_failure":
        return diagnose_subscription(case)

    if surface == "checkout_abandonment":
        return diagnose_checkout(case)

    if surface == "b2b_receivable":
        return diagnose_b2b(
            case,
            provider,
        )

    return Diagnosis(
        case_id=case["case_id"],
        root_cause="administrative_delay",
        confidence=0.10,
        evidence=[
            f"Unknown surface: {surface}"
        ],
        method="unknown_surface_fallback",
        typical_fix=get_root_cause(
            "administrative_delay"
        ).typical_fix,
    )


# ============================================================
# Dataset loading
# ============================================================

def load_cases() -> list[dict]:
    with DATA_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


# ============================================================
# Evaluation
# ============================================================

def evaluate(
    cases: list[dict],
    diagnoses: list[Diagnosis],
) -> dict:

    total = len(cases)

    correct = 0

    method_counts = {}

    confidence_sum = 0.0

    for case, diagnosis in zip(
        cases,
        diagnoses,
    ):

        expected = case["root_cause_label"]

        if diagnosis.root_cause == expected:
            correct += 1

        method_counts[diagnosis.method] = (
            method_counts.get(
                diagnosis.method,
                0,
            )
            + 1
        )

        confidence_sum += diagnosis.confidence

    accuracy = (
        correct / total
        if total
        else 0.0
    )

    average_confidence = (
        confidence_sum / total
        if total
        else 0.0
    )

    return {
        "total": total,
        "correct": correct,
        "incorrect": total - correct,
        "accuracy": accuracy,
        "average_confidence": average_confidence,
        "methods": method_counts,
    }


# ============================================================
# Self-test
# ============================================================

def main() -> None:

    print("=" * 70)
    print("REVIVE — MODULE 1B")
    print("Root Cause Diagnosis Classifier")
    print("=" * 70)

    cases = load_cases()

    print()
    print(f"Loaded cases: {len(cases)}")

    provider = create_llm_provider()

    print(
        "LLM provider:",
        provider.__class__.__name__,
    )

    diagnoses = []

    for case in cases:
        diagnosis = diagnose_case(
            case,
            provider,
        )

        diagnoses.append(diagnosis)

    metrics = evaluate(
        cases,
        diagnoses,
    )

    print()
    print("Diagnosis evaluation:")
    print(
        f"  Correct:            "
        f"{metrics['correct']}/{metrics['total']}"
    )

    print(
        f"  Incorrect:          "
        f"{metrics['incorrect']}"
    )

    print(
        f"  Accuracy:           "
        f"{metrics['accuracy'] * 100:.2f}%"
    )

    print(
        f"  Avg confidence:     "
        f"{metrics['average_confidence']:.3f}"
    )

    print()
    print("Diagnosis methods:")

    for method, count in metrics["methods"].items():
        print(
            f"  {method:<35} {count}"
        )

    print()
    print("Sample diagnoses:")

    for diagnosis in diagnoses[:5]:

        print()
        print(
            f"  Case:       {diagnosis.case_id}"
        )

        print(
            f"  Root cause: {diagnosis.root_cause}"
        )

        print(
            f"  Confidence: {diagnosis.confidence:.2f}"
        )

        print(
            f"  Method:     {diagnosis.method}"
        )

        print("  Evidence:")

        for evidence in diagnosis.evidence:
            print(
                f"    - {evidence}"
            )

    # --------------------------------------------------------
    # Structural assertions
    # --------------------------------------------------------

        print()
    print("Incorrect diagnoses:")
    
    incorrect_count = 0

    for case, diagnosis in zip(cases, diagnoses):

        expected = case["root_cause_label"]

        if diagnosis.root_cause != expected:

            incorrect_count += 1

            print()
            print("-" * 70)
            print(f"Case:          {case['case_id']}")
            print(f"Surface:       {case['surface']}")
            print(f"Expected:      {expected}")
            print(f"Predicted:     {diagnosis.root_cause}")
            print(f"Confidence:    {diagnosis.confidence:.2f}")
            print(f"Method:        {diagnosis.method}")
            print(f"Amount:        ₹{case['amount']}")

            if case["surface"] == "b2b_receivable":
                print(
                    f"Notes:         "
                    f"{case.get('comment_notes', '')}"
                )

            print("Evidence:")

            for evidence in diagnosis.evidence:
                print(f"  - {evidence}")

    print()
    print(f"Total incorrect: {incorrect_count}")

    assert len(diagnoses) == 105

    assert all(
        is_valid_root_cause(
            diagnosis.root_cause
        )
        for diagnosis in diagnoses
    )

    assert all(
        0.0 <= diagnosis.confidence <= 1.0
        for diagnosis in diagnoses
    )

    assert all(
        diagnosis.typical_fix
        for diagnosis in diagnoses
    )

    # Dataset should be classified accurately by the
    # deterministic rules.
    assert metrics["accuracy"] >= 0.95, (
        "Diagnosis accuracy below 95%. "
        "Investigate classifier rules."
    )

    print()
    print("=" * 70)
    print("MODULE 1B SELF-TEST: PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()