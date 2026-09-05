"""
Revive - Module 4
Recovery Orchestrator

Connects:

    Module 1 -> Diagnosis
    Module 3 -> Policy Engine

and converts a diagnosed revenue-risk case into a bounded
recovery action.

Important:

    This module does NOT decide whether an action is permitted.

    It proposes an action.

    The Policy Engine has final authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.policy import (
    CaseState,
    PolicyCheckResult,
    PolicyEngine,
)

from app.diagnosis.classifier import (
    Diagnosis,
    diagnose_case,
    load_cases,
)


# ============================================================
# Data classes
# ============================================================

@dataclass(frozen=True)
class RecoveryAction:
    """
    Represents a proposed recovery action and its execution
    status.
    """

    case_id: str

    action: str

    channel: str

    status: str

    reason: str

    message: str

    diagnosis: Diagnosis

    policy_result: PolicyCheckResult


# ============================================================
# Recovery Orchestrator
# ============================================================

class RecoveryOrchestrator:
    """
    Coordinates diagnosis, strategy selection and policy
    validation.

    It deliberately does not contain compliance rules.
    Those belong to PolicyEngine.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
    ) -> None:

        self.policy_engine = (
            policy_engine
            if policy_engine is not None
            else PolicyEngine()
        )

    # --------------------------------------------------------
    # Strategy selection
    # --------------------------------------------------------

    def select_strategy(
        self,
        case: dict[str, Any],
        diagnosis: Diagnosis,
    ) -> tuple[str, str, str]:
        """
        Select:

            action
            channel
            reason

        based on the diagnosed root cause and surface.
        """

        surface = case["surface"]

        root_cause = diagnosis.root_cause

        # ----------------------------------------------------
        # Subscription failures
        # ----------------------------------------------------

        if surface == "subscription_failure":

            if root_cause == "insufficient_funds":

                return (
                    "payment_retry",
                    "payment_gateway",
                    (
                        "Insufficient funds may be temporary; "
                        "a bounded retry is appropriate."
                    ),
                )

            if root_cause == "otp_timeout":

                return (
                    "whatsapp",
                    "whatsapp",
                    (
                        "Customer authentication timed out; "
                        "a guided retry prompt is appropriate."
                    ),
                )

            if root_cause == "issuer_declined":

                return (
                    "payment_retry",
                    "payment_gateway",
                    (
                        "Issuer decline may be transient; "
                        "a bounded retry can be attempted."
                    ),
                )

            if root_cause == "card_expired":

                return (
                    "whatsapp",
                    "whatsapp",
                    (
                        "The customer's payment method has "
                        "expired and should be updated."
                    ),
                )

            if root_cause == "mandate_expired_or_revoked":

                return (
                    "whatsapp",
                    "whatsapp",
                    (
                        "The recurring mandate is no longer "
                        "valid and requires re-authorization."
                    ),
                )

            if root_cause == "mandate_debit_failed":

                return (
                    "payment_retry",
                    "payment_gateway",
                    (
                        "The mandate itself is still valid; only "
                        "this debit attempt failed. Re-sequence "
                        "the retry through the mandate retry "
                        "sequencer (pre-debit notice, per-cycle "
                        "attempt cap) rather than treating it as "
                        "a generic card retry."
                    ),
                )

            if root_cause == "network_error":

                return (
                    "payment_retry",
                    "payment_gateway",
                    (
                        "A network failure can be transient; "
                        "retry is appropriate within policy."
                    ),
                )

        # ----------------------------------------------------
        # Checkout abandonment
        # ----------------------------------------------------

        if surface == "checkout_abandonment":

            return (
                "email",
                "email",
                (
                    "The customer abandoned checkout; "
                    "a recovery checkout message is appropriate."
                ),
            )

        # ----------------------------------------------------
        # B2B receivables
        # ----------------------------------------------------

        if surface == "b2b_receivable":

            if root_cause == "invoice_dispute":

                return (
                    "human_escalation",
                    "human_finance",
                    (
                        "The invoice is disputed; automated "
                        "negotiation must not be used."
                    ),
                )

            if root_cause == "b2b_cashflow_delay":

                return (
                    "negotiate",
                    "voice_call",
                    (
                        "Customer indicates cash-flow pressure; "
                        "a bounded settlement negotiation is "
                        "appropriate."
                    ),
                )

            if root_cause == "payment_approval_delay":

                return (
                    "voice_call",
                    "voice_call",
                    (
                        "Payment is waiting for internal approval; "
                        "accounts-team follow-up is appropriate."
                    ),
                )

            if root_cause == "administrative_delay":

                return (
                    "voice_call",
                    "voice_call",
                    (
                        "Administrative processing is delayed; "
                        "structured finance follow-up is appropriate."
                    ),
                )

        # ----------------------------------------------------
        # Safe default
        # ----------------------------------------------------

        return (
            "human_escalation",
            "human",
            (
                "No sufficiently specific automated strategy "
                "was identified; route to human review."
            ),
        )

    # --------------------------------------------------------
    # Message generation
    # --------------------------------------------------------

    def generate_message(
        self,
        case: dict[str, Any],
        diagnosis: Diagnosis,
        action: str,
    ) -> str:
        """
        Generate a deterministic recovery message.

        This is intentionally not an LLM call.

        We want recovery execution to remain predictable.
        """

        customer_name = case.get(
            "customer_name",
            "Customer",
        )

        amount = case.get(
            "amount",
            0,
        )

        root_cause = diagnosis.root_cause

        if action == "payment_retry":

            if root_cause == "mandate_debit_failed":

                return (
                    f"Hello {customer_name}, your scheduled "
                    f"autopay debit of ₹{amount:,} could not be "
                    "completed, most likely due to insufficient "
                    "balance on the debit date. Your mandate is "
                    "still active — we will notify you before "
                    "the next permitted retry, within your "
                    "bank's mandate retry limits."
                )

            return (
                f"Hello {customer_name}, we noticed that your "
                f"payment of ₹{amount:,} could not be completed. "
                "We will make one permitted retry shortly. "
                "If the issue continues, please use another "
                "payment method."
            )

        if action == "whatsapp":

            if root_cause == "card_expired":

                return (
                    f"Hello {customer_name}, your saved payment "
                    "card appears to have expired. Please update "
                    "your payment method to continue your "
                    "subscription."
                )

            if root_cause == "mandate_expired_or_revoked":

                return (
                    f"Hello {customer_name}, your recurring payment "
                    "authorization is no longer active. Please "
                    "reauthorize the mandate to continue."
                )

            if root_cause == "otp_timeout":

                return (
                    f"Hello {customer_name}, your payment could "
                    "not be completed because authentication "
                    "timed out. Please try again."
                )

            return (
                f"Hello {customer_name}, we noticed an issue "
                "with your recent payment. Please try again "
                "using the secure payment option provided."
            )

        if action == "email":

            return (
                f"Hi {customer_name}, your recent checkout for "
                f"₹{amount:,} was not completed. Your order may "
                "still be available. Please return to checkout "
                "to complete your purchase."
            )

        if action == "voice_call":

            return (
                f"Finance follow-up for {customer_name}: "
                f"₹{amount:,} remains outstanding. "
                "Discuss payment status and expected settlement "
                "date without exceeding configured policy limits."
            )

        if action == "negotiate":

            return (
                f"Settlement discussion with {customer_name} "
                f"for ₹{amount:,}. Start with the full amount "
                "and no discount. Only introduce a permitted "
                "fallback if required by the negotiation."
            )

        if action == "human_escalation":

            return (
                f"Escalate case for {customer_name}, amount "
                f"₹{amount:,}, to the appropriate human finance "
                "team for review."
            )

        return (
            "No automated message generated."
        )

    # --------------------------------------------------------
    # Execute action
    # --------------------------------------------------------

    def execute_action(
        self,
        case: dict[str, Any],
        diagnosis: Diagnosis,
        state: CaseState,
        now: datetime,
    ) -> RecoveryAction:

        action, channel, strategy_reason = (
            self.select_strategy(
                case,
                diagnosis,
            )
        )

        policy_result = (
            self.policy_engine.check_action(
                state=state,
                action=action,
                now=now,
            )
        )

        message = self.generate_message(
            case,
            diagnosis,
            action,
        )

        if policy_result.allowed:

            status = "APPROVED"

            reason = (
                f"{strategy_reason} "
                "Policy checks passed."
            )

        else:

            status = "BLOCKED"

            reason = (
                f"{strategy_reason} "
                "Policy blocked the action: "
                + "; ".join(
                    policy_result.blocking_reasons
                )
            )

        return RecoveryAction(
            case_id=case["case_id"],
            action=action,
            channel=channel,
            status=status,
            reason=reason,
            message=message,
            diagnosis=diagnosis,
            policy_result=policy_result,
        )

    # --------------------------------------------------------
    # Process case
    # --------------------------------------------------------

    def process_case(
        self,
        case: dict[str, Any],
        state: CaseState | None = None,
        now: datetime | None = None,
    ) -> RecoveryAction:

        if state is None:

            state = CaseState(
                case_id=case["case_id"]
            )

        if now is None:

            now = datetime.fromisoformat(
                case["timestamp"]
            )

        diagnosis = diagnose_case(
            case
        )

        return self.execute_action(
            case=case,
            diagnosis=diagnosis,
            state=state,
            now=now,
        )


# ============================================================
# Batch helper
# ============================================================

def run_batch(
    cases: list[dict[str, Any]],
) -> list[RecoveryAction]:

    orchestrator = RecoveryOrchestrator()

    results = []

    for case in cases:

        result = orchestrator.process_case(
            case
        )

        results.append(result)

    return results


# ============================================================
# Self-test
# ============================================================

def main() -> None:

    print("=" * 72)
    print("REVIVE — MODULE 4")
    print("Recovery Orchestrator")
    print("=" * 72)

    cases = load_cases()

    print()
    print(
        f"Loaded cases: {len(cases)}"
    )

    results = run_batch(
        cases
    )

    approved = [
        result
        for result in results
        if result.status == "APPROVED"
    ]

    blocked = [
        result
        for result in results
        if result.status == "BLOCKED"
    ]

    print()
    print("Batch result:")
    print(
        f"  Total cases:       {len(results)}"
    )

    print(
        f"  Approved actions:  {len(approved)}"
    )

    print(
        f"  Blocked actions:   {len(blocked)}"
    )

    print()
    print("Action distribution:")

    action_counts: dict[str, int] = {}

    for result in results:

        action_counts[result.action] = (
            action_counts.get(
                result.action,
                0,
            )
            + 1
        )

    for action, count in sorted(
        action_counts.items()
    ):

        print(
            f"  {action:<20} {count}"
        )

    # --------------------------------------------------------
    # Sample results
    # --------------------------------------------------------

    print()
    print("Sample recovery decisions:")

    for result in results[:10]:

        print()
        print(
            f"  Case:       {result.case_id}"
        )

        print(
            f"  Diagnosis:  "
            f"{result.diagnosis.root_cause}"
        )

        print(
            f"  Action:     "
            f"{result.action}"
        )

        print(
            f"  Channel:    "
            f"{result.channel}"
        )

        print(
            f"  Status:     "
            f"{result.status}"
        )

        print(
            f"  Reason:     "
            f"{result.reason}"
        )

    # --------------------------------------------------------
    # Verify disputed B2B cases never negotiate
    # --------------------------------------------------------

    disputed_results = []

    for case, result in zip(
        cases,
        results,
    ):

        if (
            case["surface"]
            == "b2b_receivable"
            and case["root_cause_label"]
            == "invoice_dispute"
        ):

            disputed_results.append(
                result
            )

    print()
    print(
        "Disputed B2B safety check:"
    )

    print(
        f"  Disputed cases: "
        f"{len(disputed_results)}"
    )

    assert disputed_results

    assert all(
        result.action
        != "negotiate"
        for result in disputed_results
    )

    assert all(
        result.action
        == "human_escalation"
        for result in disputed_results
    )

    print(
        "  ✓ Disputed cases routed to human escalation."
    )

    # --------------------------------------------------------
    # Verify blocked actions contain explanations
    # --------------------------------------------------------

    for result in blocked:

        assert result.reason

        assert result.policy_result.blocking_reasons

        assert all(
            reason
            for reason
            in result.policy_result.blocking_reasons
        )

    print()
    print(
        "Blocked-action explanation check:"
    )

    print(
        f"  ✓ All {len(blocked)} blocked actions "
        "contain explicit reasons."
    )

    # --------------------------------------------------------
    # Verify every result has required evidence
    # --------------------------------------------------------

    for result in results:

        assert result.diagnosis.root_cause

        assert result.diagnosis.evidence

        assert result.policy_result.checks

        assert result.reason

    print()
    print(
        "Explainability check:"
    )

    print(
        "  ✓ Diagnosis evidence present."
    )

    print(
        "  ✓ Policy checks present."
    )

    print(
        "  ✓ Decision reason present."
    )

    # --------------------------------------------------------
    # Final
    # --------------------------------------------------------

    assert len(results) == 105

    print()
    print("=" * 72)
    print("MODULE 4 SELF-TEST: PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()