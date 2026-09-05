"""
Revive - Module 6
Core ROI Portfolio Engine v4

Policy-Gated Economic Recovery

Architecture:

    Diagnosis
        ↓
    Strategy
        ↓
    Policy Gate
        ↓
    BLOCKED ───────────────→ STOP
        │
        ▼
      ALLOWED
        ↓
    ROI / Expected Value
        ↓
    PURSUE / STOP
        ↓
    Synthetic Outcome
        ↓
    Recovery Ledger
        ↓
    Continue / Stop

IMPORTANT:

The Policy Engine is the authoritative safety boundary.

ROI must NEVER override a policy decision.

The ROI engine does not use an LLM.

Synthetic outcomes exist only for deterministic
buildathon benchmarking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.policy import (
    CaseState,
    PolicyCheckResult,
    PolicyEngine,
)

from app.diagnosis.classifier import (
    Diagnosis,
    diagnose_case,
)

from app.orchestrator.orchestrator import (
    RecoveryOrchestrator,
)

from app.recovery_ledger.ledger import (
    DECISION_PURSUED,
    DECISION_STOPPED,
    OUTCOME_NOT_RECOVERED,
    OUTCOME_RECOVERED,
    RecoveryEvent,
    RecoveryLedger,
)


# ============================================================
# Constants
# ============================================================

EXPECTED_CASE_COUNT = 105

EPSILON = 1e-9


# ============================================================
# Data Classes
# ============================================================

@dataclass(frozen=True)
class ROIExplanation:
    """
    Complete economic explanation for one recovery attempt.
    """

    base_success_probability: float

    root_cause_multiplier: float

    attempt_decay_multiplier: float

    final_success_probability: float

    recoverable_amount: float

    action_cost: float

    expected_recovery: float

    expected_value: float

    reason: str


@dataclass(frozen=True)
class ROIDecision:
    """
    Immutable result of one ROI attempt.

    The policy result is deliberately carried forward into the
    decision so that the Recovery Ledger can preserve the exact
    safety evidence used for this decision.
    """

    case_id: str

    amount: float

    root_cause: str

    action: str

    channel: str

    attempt_number: int

    decision: str

    explanation: ROIExplanation

    outcome: str

    recovered_amount: float

    # --------------------------------------------------------
    # Authoritative policy evidence
    # --------------------------------------------------------

    policy_allowed: bool

    policy_blocking_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioMetrics:
    """
    Portfolio-level ROI metrics.
    """

    total_cases: int

    addressable_amount: float

    pursued_attempts: int

    stopped_cases: int

    recovered_cases: int

    recovered_amount: float

    unrecovered_amount: float

    recovery_cost: float

    net_recovered_value: float

    recovery_rate: float

    cost_per_rupee_recovered: float


@dataclass(frozen=True)
class StrategyComparison:
    """
    Comparison between naive recovery and Revive ROI recovery.
    """

    naive_attempts: int

    naive_cost: float

    naive_recovered_amount: float

    revive_attempts: int

    revive_cost: float

    revive_recovered_amount: float

    cost_savings: float

    additional_cost: float

    additional_recovery: float

    def summary_line(self) -> str:
        """
        Human-readable strategy comparison.
        """

        if self.additional_cost > 0:
            return (
                f"Revive recovered an additional "
                f"₹{self.additional_recovery:,.2f} for "
                f"₹{self.additional_cost:,.2f} more in "
                f"recovery cost than the naive strategy."
            )

        return (
            f"Revive recovered an additional "
            f"₹{self.additional_recovery:,.2f} while also "
            f"costing ₹{abs(self.additional_cost):,.2f} less "
            f"than the naive strategy."
        )


# ============================================================
# ROI Portfolio Engine
# ============================================================

class ROIPortfolioEngine:
    """
    Economic decision engine for the Revive recovery portfolio.

    Policy is always evaluated before ROI.

    Pipeline:

        case
          ↓
        diagnosis
          ↓
        strategy
          ↓
        policy
          ↓
        ROI
    """

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        ledger: RecoveryLedger | None = None,
    ) -> None:

        # ----------------------------------------------------
        # Shared policy engine
        # ----------------------------------------------------

        self.policy_engine = (
            policy_engine
            if policy_engine is not None
            else PolicyEngine()
        )

        # ----------------------------------------------------
        # Strategy orchestrator
        # ----------------------------------------------------

        self.orchestrator = RecoveryOrchestrator(
            policy_engine=self.policy_engine
        )

        # ----------------------------------------------------
        # Authoritative ledger
        # ----------------------------------------------------

        self.ledger = (
            ledger
            if ledger is not None
            else RecoveryLedger()
        )

        # ----------------------------------------------------
        # ROI configuration
        # ----------------------------------------------------

        policy = self.policy_engine.policy

        self.action_costs = {
            key: float(value)
            for key, value in policy[
                "channel_cost_inr"
            ].items()
        }

        self.action_priors = {
            key: float(value)
            for key, value in policy[
                "channel_success_prior"
            ].items()
        }

        self.root_cause_multipliers = {
            key: float(value)
            for key, value in policy[
                "root_cause_success_multiplier"
            ].items()
        }

        self.attempt_decay = float(
            policy["roi_attempt_decay"]
        )

        self.max_attempts = int(
            policy["retry"]["max_attempts"]
        )

        # ----------------------------------------------------
        # Configuration validation
        # ----------------------------------------------------

        if not 0.0 < self.attempt_decay < 1.0:
            raise ValueError(
                "roi_attempt_decay must be between 0 and 1."
            )

        if self.max_attempts < 1:
            raise ValueError(
                "retry.max_attempts must be >= 1."
            )

        for key, cost in self.action_costs.items():
            if cost < 0:
                raise ValueError(
                    f"Action cost cannot be negative: {key}"
                )

        for key, probability in self.action_priors.items():
            if not 0.0 <= probability <= 1.0:
                raise ValueError(
                    "Channel success prior must be between "
                    f"0 and 1: {key}"
                )

        for key, multiplier in self.root_cause_multipliers.items():
            if multiplier < 0:
                raise ValueError(
                    "Root-cause multiplier cannot be negative: "
                    f"{key}"
                )

    # ========================================================
    # Case → Policy State
    # ========================================================

    def build_case_state(
        self,
        case: dict[str, Any],
    ) -> CaseState:
        """
        Convert raw case data into authoritative PolicyEngine
        CaseState.
        """

        case_id = case.get("case_id")

        if not case_id:
            raise ValueError(
                "Case must contain a non-empty case_id."
            )

        # ----------------------------------------------------
        # Last contact timestamp
        # ----------------------------------------------------

        last_contact_at = None

        raw_last_contact = case.get(
            "last_contact_at"
        )

        if raw_last_contact:

            if isinstance(
                raw_last_contact,
                datetime,
            ):
                last_contact_at = raw_last_contact

            else:
                last_contact_at = datetime.fromisoformat(
                    str(raw_last_contact)
                )

        # ----------------------------------------------------
        # Promise date
        # ----------------------------------------------------

        promise_date = None

        raw_promise_date = case.get(
            "promise_date"
        )

        if raw_promise_date:

            if isinstance(
                raw_promise_date,
                datetime,
            ):
                promise_date = raw_promise_date

            else:
                promise_date = datetime.fromisoformat(
                    str(raw_promise_date)
                )

        # ----------------------------------------------------
        # Build authoritative policy state
        # ----------------------------------------------------

        return CaseState(
            case_id=case_id,

            contact_attempts=int(
                case.get(
                    "contact_attempts",
                    0,
                )
            ),

            last_contact_at=last_contact_at,

            promise_to_pay_active=bool(
                case.get(
                    "promise_to_pay_active",
                    False,
                )
            ),

            promise_date=promise_date,

            disputed=bool(
                case.get(
                    "disputed",
                    False,
                )
            ),

            opted_out=bool(
                case.get(
                    "opted_out",
                    False,
                )
            ),

            negotiation_rounds=int(
                case.get(
                    "negotiation_rounds",
                    0,
                )
            ),

            metadata=case.get(
                "metadata",
                {},
            ),
        )

    # ========================================================
    # Policy Gate
    # ========================================================

    def check_policy(
        self,
        case: dict[str, Any],
        action: str,
        attempt_number: int = 1,
    ) -> PolicyCheckResult:
        """
        Run the authoritative policy engine.

        ROI is not calculated here.
        """

        if attempt_number < 1:
            raise ValueError(
                "attempt_number must be >= 1."
            )

        state = self.build_case_state(
            case
        )

        timestamp = case.get(
            "timestamp"
        )

        if timestamp:

            if isinstance(
                timestamp,
                datetime,
            ):
                now = timestamp

            else:
                now = datetime.fromisoformat(
                    str(timestamp)
                )

        else:
            now = datetime.now()

        return self.policy_engine.check_action(
            state=state,
            action=action,
            now=now,
        )

    # ========================================================
    # Pricing
    # ========================================================

    def pricing_key(
        self,
        action: str,
        channel: str,
    ) -> str:
        """
        Translate strategy action into policy pricing key.
        """

        if action == "negotiate":
            return "voice_call"

        if action == "human_escalation":
            return "human_escalation"

        return action

    # --------------------------------------------------------

    def action_cost(
        self,
        action: str,
        channel: str,
    ) -> float:
        """
        Return configured action cost.
        """

        key = self.pricing_key(
            action,
            channel,
        )

        if key not in self.action_costs:
            raise ValueError(
                "No ROI cost configured for "
                f"action='{action}', "
                f"channel='{channel}', "
                f"policy_key='{key}'."
            )

        return self.action_costs[key]

    # ========================================================
    # Success Prior
    # ========================================================

    def action_success_prior(
        self,
        action: str,
        channel: str,
    ) -> float:
        """
        Return configured base success probability.
        """

        key = self.pricing_key(
            action,
            channel,
        )

        if key not in self.action_priors:
            raise ValueError(
                "No ROI success prior configured for "
                f"action='{action}', "
                f"channel='{channel}', "
                f"policy_key='{key}'."
            )

        probability = self.action_priors[key]

        if not 0.0 <= probability <= 1.0:
            raise ValueError(
                f"Invalid success prior for '{key}': "
                f"{probability}"
            )

        return probability

    # ========================================================
    # Success Probability
    # ========================================================

    def calculate_success_probability(
        self,
        root_cause: str,
        action: str,
        channel: str,
        attempt_number: int,
    ) -> tuple[
        float,
        float,
        float,
        float,
    ]:
        """
        Calculate probability for one attempt.

        Formula:

            base prior
                × root-cause multiplier
                × attempt decay
        """

        if attempt_number < 1:
            raise ValueError(
                "attempt_number must be >= 1."
            )

        base_probability = self.action_success_prior(
            action,
            channel,
        )

        root_multiplier = (
            self.root_cause_multipliers.get(
                root_cause,
                1.0,
            )
        )

        decay_multiplier = (
            self.attempt_decay
            ** (attempt_number - 1)
        )

        probability = (
            base_probability
            * root_multiplier
            * decay_multiplier
        )

        probability = max(
            0.0,
            min(
                probability,
                1.0,
            ),
        )

        return (
            base_probability,
            root_multiplier,
            decay_multiplier,
            probability,
        )

    # ========================================================
    # Policy-Blocked Decision
    # ========================================================

    def create_policy_blocked_decision(
        self,
        case: dict[str, Any],
        diagnosis: Diagnosis,
        attempt_number: int,
        policy_result: PolicyCheckResult,
    ) -> ROIDecision:
        """
        Create STOP decision when policy blocks the action.

        A policy-blocked action does not enter economic execution.

        Therefore:

            expected recovery = 0
            action cost = 0
            expected value = 0
            outcome = NOT_RECOVERED
        """

        amount = float(
            case["amount"]
        )

        if amount < 0:
            raise ValueError(
                f"Case amount cannot be negative: "
                f"{case['case_id']}"
            )

        blocking_reason = "; ".join(
            policy_result.blocking_reasons
        )

        reason = (
            f"Policy blocked action "
            f"'{policy_result.action}'. "
            f"Attempt #{attempt_number}. "
            f"Reason: {blocking_reason}"
        )

        explanation = ROIExplanation(
            base_success_probability=0.0,

            root_cause_multiplier=0.0,

            attempt_decay_multiplier=0.0,

            final_success_probability=0.0,

            recoverable_amount=amount,

            action_cost=0.0,

            expected_recovery=0.0,

            expected_value=0.0,

            reason=reason,
        )

        return ROIDecision(
            case_id=case["case_id"],

            amount=amount,

            root_cause=diagnosis.root_cause,

            action=policy_result.action,

            channel=self._channel_for_action(
                policy_result.action
            ),

            attempt_number=attempt_number,

            decision=DECISION_STOPPED,

            explanation=explanation,

            outcome=OUTCOME_NOT_RECOVERED,

            recovered_amount=0.0,

            # ------------------------------------------------
            # IMPORTANT:
            # Preserve authoritative policy evidence.
            # ------------------------------------------------

            policy_allowed=policy_result.allowed,

            policy_blocking_reasons=tuple(
                policy_result.blocking_reasons
            ),
        )

    # ========================================================
    # Channel helper
    # ========================================================

    def _channel_for_action(
        self,
        action: str,
    ) -> str:
        """
        Convert action to corresponding channel.
        """

        mapping = {
            "payment_retry": "payment_gateway",
            "whatsapp": "whatsapp",
            "email": "email",
            "voice_call": "voice_call",
            "negotiate": "voice_call",
            "human_escalation": "human_finance",
            "stop": "none",
        }

        return mapping.get(
            action,
            action,
        )

    # ========================================================
    # Evaluate One Attempt
    # ========================================================

    def evaluate_attempt(
        self,
        case: dict[str, Any],
        diagnosis: Diagnosis,
        attempt_number: int,
    ) -> ROIDecision:
        """
        Evaluate one recovery attempt.

        Lifecycle:

            Strategy
                ↓
            Policy Gate
                ↓
            ROI
                ↓
            Decision
                ↓
            Synthetic Outcome
        """

        if attempt_number < 1:
            raise ValueError(
                "attempt_number must be >= 1."
            )

        # ----------------------------------------------------
        # Strategy
        # ----------------------------------------------------

        action, channel, strategy_reason = (
            self.orchestrator.select_strategy(
                case,
                diagnosis,
            )
        )

        # ----------------------------------------------------
        # POLICY GATE
        # ----------------------------------------------------

        policy_result = self.check_policy(
            case=case,
            action=action,
            attempt_number=attempt_number,
        )

        # ----------------------------------------------------
        # Policy has absolute authority.
        # ----------------------------------------------------

        if not policy_result.allowed:

            return self.create_policy_blocked_decision(
                case=case,
                diagnosis=diagnosis,
                attempt_number=attempt_number,
                policy_result=policy_result,
            )

        # ----------------------------------------------------
        # Economic evaluation happens only after policy passes.
        # ----------------------------------------------------

        amount = float(
            case["amount"]
        )

        if amount < 0:
            raise ValueError(
                f"Case amount cannot be negative: "
                f"{case['case_id']}"
            )

        (
            base_probability,
            root_multiplier,
            decay_multiplier,
            probability,
        ) = self.calculate_success_probability(
            root_cause=diagnosis.root_cause,
            action=action,
            channel=channel,
            attempt_number=attempt_number,
        )

        cost = self.action_cost(
            action,
            channel,
        )

        expected_recovery = (
            probability
            * amount
        )

        expected_value = (
            expected_recovery
            - cost
        )

        # ----------------------------------------------------
        # Economic decision
        # ----------------------------------------------------

        if expected_value > 0.0:

            decision = DECISION_PURSUED

            reason = (
                f"{strategy_reason} "
                f"Policy checks passed. "
                f"Attempt #{attempt_number}: "
                f"P(success)={probability:.2%}, "
                f"expected recovery="
                f"₹{expected_recovery:,.2f}, "
                f"action cost="
                f"₹{cost:,.2f}, "
                f"expected value="
                f"₹{expected_value:,.2f}. "
                "Expected value is positive, "
                "so pursue the action."
            )

        else:

            decision = DECISION_STOPPED

            reason = (
                f"{strategy_reason} "
                f"Policy checks passed. "
                f"Attempt #{attempt_number}: "
                f"P(success)={probability:.2%}, "
                f"expected recovery="
                f"₹{expected_recovery:,.2f}, "
                f"action cost="
                f"₹{cost:,.2f}, "
                f"expected value="
                f"₹{expected_value:,.2f}. "
                "Expected value is non-positive, "
                "so stop further automated recovery."
            )

        # ----------------------------------------------------
        # Synthetic benchmark outcome
        # ----------------------------------------------------

        outcome = self.simulate_outcome(
            case=case,
            diagnosis=diagnosis,
            decision=decision,
            attempt_number=attempt_number,
        )

        recovered_amount = (
            amount
            if outcome == OUTCOME_RECOVERED
            else 0.0
        )

        # ----------------------------------------------------
        # Financial integrity
        # ----------------------------------------------------

        assert (
            0.0
            <= probability
            <= 1.0
        )

        assert (
            0.0
            <= expected_recovery
            <= amount
        )

        assert cost >= 0.0

        assert (
            abs(
                expected_value
                - (
                    expected_recovery
                    - cost
                )
            )
            < 0.000001
        )

        assert (
            0.0
            <= recovered_amount
            <= amount
        )

        explanation = ROIExplanation(
            base_success_probability=(
                base_probability
            ),

            root_cause_multiplier=(
                root_multiplier
            ),

            attempt_decay_multiplier=(
                decay_multiplier
            ),

            final_success_probability=(
                probability
            ),

            recoverable_amount=amount,

            action_cost=cost,

            expected_recovery=(
                expected_recovery
            ),

            expected_value=(
                expected_value
            ),

            reason=reason,
        )

        return ROIDecision(
            case_id=case["case_id"],

            amount=amount,

            root_cause=diagnosis.root_cause,

            action=action,

            channel=channel,

            attempt_number=attempt_number,

            decision=decision,

            explanation=explanation,

            outcome=outcome,

            recovered_amount=recovered_amount,

            # ------------------------------------------------
            # Policy passed, therefore this decision carries
            # the successful policy gate evidence.
            # ------------------------------------------------

            policy_allowed=policy_result.allowed,

            policy_blocking_reasons=tuple(
                policy_result.blocking_reasons
            ),
        )

    # ========================================================
    # Synthetic Benchmark Outcome
    # ========================================================

    def simulate_outcome(
        self,
        case: dict[str, Any],
        diagnosis: Diagnosis,
        decision: str,
        attempt_number: int,
    ) -> str:
        """
        Deterministic synthetic outcome simulator.

        ONLY for reproducible buildathon benchmarking.

        STOP can never recover.
        """

        if decision == DECISION_STOPPED:
            return OUTCOME_NOT_RECOVERED

        case_score = sum(
            ord(character)
            for character
            in case["case_id"]
        )

        root_score = sum(
            ord(character)
            for character
            in diagnosis.root_cause
        )

        surface_score = sum(
            ord(character)
            for character
            in case["surface"]
        )

        score = (
            case_score
            + root_score
            + surface_score
            + attempt_number * 17
        ) % 100

        thresholds = {
            "insufficient_funds": 65,
            "otp_timeout": 72,
            "issuer_declined": 48,
            "card_expired": 78,
            "mandate_expired_or_revoked": 74,
            "mandate_debit_failed": 68,
            "network_error": 60,
            "invoice_dispute": 20,
            "b2b_cashflow_delay": 62,
            "payment_approval_delay": 68,
            "administrative_delay": 55,
            "checkout_abandonment": 52,
        }

        threshold = thresholds.get(
            diagnosis.root_cause,
            50,
        )

        if score < threshold:
            return OUTCOME_RECOVERED

        return OUTCOME_NOT_RECOVERED

    # ========================================================
    # Record Decision
    # ========================================================

    def _record_decision(
        self,
        decision: ROIDecision,
        case: dict[str, Any],
    ) -> RecoveryEvent:
        """
        Convert one decision into exactly one ledger event.

        IMPORTANT:

        Policy evidence is preserved in the ledger.

        A blocked policy action must remain visibly blocked
        in downstream dashboard and explainability layers.
        """

        # --------------------------------------------------------
        # Reuse the authoritative policy result for this exact
        # action/attempt.
        # --------------------------------------------------------

        policy_result = self.check_policy(
            case=case,
            action=decision.action,
            attempt_number=decision.attempt_number,
        )

        # --------------------------------------------------------
        # Convert blocking reasons to immutable tuple.
        # --------------------------------------------------------

        blocking_reasons = tuple(
            policy_result.blocking_reasons
        )

        # --------------------------------------------------------
        # Create immutable ledger event.
        # --------------------------------------------------------

        event = RecoveryEvent(

            case_id=decision.case_id,

            attempt_number=(
                decision.attempt_number
            ),

            timestamp=str(
                case["timestamp"]
            ),

            action=decision.action,

            channel=decision.channel,

            amount=decision.amount,

            success_probability=(
                decision
                .explanation
                .final_success_probability
            ),

            expected_recovery=(
                decision
                .explanation
                .expected_recovery
            ),

            action_cost=(
                decision
                .explanation
                .action_cost
            ),

            expected_value=(
                decision
                .explanation
                .expected_value
            ),

            decision=decision.decision,

            outcome=decision.outcome,

            reason=decision.explanation.reason,

            # ----------------------------------------------------
            # IMPORTANT POLICY EVIDENCE
            # ----------------------------------------------------

            policy_allowed=(
                policy_result.allowed
            ),

            policy_blocking_reasons=(
                blocking_reasons
            ),
        )

        # --------------------------------------------------------
        # Single authoritative ledger write.
        # --------------------------------------------------------

        self.ledger.record(
            event
        )

        return event

    # ========================================================
    # Run One Case
    # ========================================================

    def run_case(
        self,
        case: dict[str, Any],
    ) -> list[ROIDecision]:
        """
        Run marginal-EV recovery lifecycle for one case.
        """

        case_id = case.get(
            "case_id"
        )

        if not case_id:
            raise ValueError(
                "Case must contain a non-empty case_id."
            )

        diagnosis = diagnose_case(
            case
        )

        decisions: list[ROIDecision] = []

        for attempt_number in range(
            1,
            self.max_attempts + 1,
        ):

            # ------------------------------------------------
            # Never continue after recovery.
            # ------------------------------------------------

            if self.ledger.has_recovered(
                case_id
            ):
                break

            # ------------------------------------------------
            # Evaluate current attempt.
            # ------------------------------------------------

            decision = self.evaluate_attempt(
                case=case,
                diagnosis=diagnosis,
                attempt_number=attempt_number,
            )

            # ------------------------------------------------
            # Record exactly once.
            # ------------------------------------------------

            self._record_decision(
                decision=decision,
                case=case,
            )

            decisions.append(
                decision
            )

            # ------------------------------------------------
            # Successful recovery ends lifecycle.
            # ------------------------------------------------

            if (
                decision.outcome
                == OUTCOME_RECOVERED
            ):
                break

            # ------------------------------------------------
            # STOP ends lifecycle.
            # ------------------------------------------------

            if (
                decision.decision
                == DECISION_STOPPED
            ):
                break

        return decisions

    # ========================================================
    # Run Portfolio
    # ========================================================

    def run_portfolio(
        self,
        cases: list[dict[str, Any]],
    ) -> list[ROIDecision]:
        """
        Run ROI recovery over every case.
        """

        if not cases:
            raise ValueError(
                "ROI portfolio cannot process an empty case list."
            )

        all_decisions: list[ROIDecision] = []

        seen_case_ids: set[str] = set()

        for case in cases:

            case_id = case.get(
                "case_id"
            )

            if not case_id:
                raise ValueError(
                    "Every case must contain case_id."
                )

            if case_id in seen_case_ids:
                raise ValueError(
                    f"Duplicate case_id detected: {case_id}"
                )

            seen_case_ids.add(
                case_id
            )

            case_decisions = self.run_case(
                case
            )

            if not case_decisions:
                raise AssertionError(
                    "ROI engine produced no decision for "
                    f"case {case_id}."
                )

            all_decisions.extend(
                case_decisions
            )

        return all_decisions

    # ========================================================
    # Metrics
    # ========================================================

    def calculate_metrics(
        self,
        decisions: list[ROIDecision],
        cases: list[dict[str, Any]],
    ) -> PortfolioMetrics:
        """
        Calculate portfolio economics.

        Only pursued actions generate recovery cost.
        """

        total_cases = len(
            cases
        )

        addressable = sum(
            float(case["amount"])
            for case in cases
        )

        pursued_attempts = sum(
            1
            for decision in decisions
            if decision.decision
            == DECISION_PURSUED
        )

        stopped_case_ids = {
            decision.case_id
            for decision in decisions
            if decision.decision
            == DECISION_STOPPED
        }

        recovered_case_ids = {
            decision.case_id
            for decision in decisions
            if decision.outcome
            == OUTCOME_RECOVERED
        }

        recovered_amount = sum(
            float(case["amount"])
            for case in cases
            if case["case_id"]
            in recovered_case_ids
        )

        unrecovered = max(
            0.0,
            addressable
            - recovered_amount,
        )

        recovery_cost = sum(
            float(
                decision.explanation.action_cost
            )
            for decision in decisions
            if decision.decision
            == DECISION_PURSUED
        )

        net_value = (
            recovered_amount
            - recovery_cost
        )

        recovery_rate = (
            recovered_amount
            / addressable
            if addressable > 0
            else 0.0
        )

        cost_per_rupee = (
            recovery_cost
            / recovered_amount
            if recovered_amount > 0
            else 0.0
        )

        return PortfolioMetrics(
            total_cases=total_cases,

            addressable_amount=(
                addressable
            ),

            pursued_attempts=(
                pursued_attempts
            ),

            stopped_cases=len(
                stopped_case_ids
            ),

            recovered_cases=len(
                recovered_case_ids
            ),

            recovered_amount=(
                recovered_amount
            ),

            unrecovered_amount=(
                unrecovered
            ),

            recovery_cost=(
                recovery_cost
            ),

            net_recovered_value=(
                net_value
            ),

            recovery_rate=(
                recovery_rate
            ),

            cost_per_rupee_recovered=(
                cost_per_rupee
            ),
        )

    # ========================================================
    # Naive Baseline
    # ========================================================

    def calculate_naive_baseline(
        self,
        cases: list[dict[str, Any]],
    ) -> StrategyComparison:
        """
        Calculate deterministic naive strategy.

        Naive:

            pursue every available automated action
            until recovery or max attempts.

        Baseline does not write to this ledger.
        """

        naive_attempts = 0

        naive_cost = 0.0

        naive_recovered = 0.0

        for case in cases:

            diagnosis = diagnose_case(
                case
            )

            action, channel, _ = (
                self.orchestrator.select_strategy(
                    case,
                    diagnosis,
                )
            )

            # ------------------------------------------------
            # Human escalation excluded from automated baseline.
            # ------------------------------------------------

            if action == "human_escalation":
                continue

            # ------------------------------------------------
            # Respect policy in baseline as well.
            # ------------------------------------------------

            policy_result = self.check_policy(
                case=case,
                action=action,
                attempt_number=1,
            )

            if not policy_result.allowed:
                continue

            cost = self.action_cost(
                action,
                channel,
            )

            for attempt in range(
                1,
                self.max_attempts + 1,
            ):

                # ------------------------------------------------
                # Re-check policy for every attempt.
                # ------------------------------------------------

                attempt_policy = (
                    self.check_policy(
                        case=case,
                        action=action,
                        attempt_number=attempt,
                    )
                )

                if not attempt_policy.allowed:
                    break

                naive_attempts += 1

                naive_cost += cost

                outcome = self.simulate_outcome(
                    case=case,
                    diagnosis=diagnosis,
                    decision=DECISION_PURSUED,
                    attempt_number=attempt,
                )

                if (
                    outcome
                    == OUTCOME_RECOVERED
                ):

                    naive_recovered += float(
                        case["amount"]
                    )

                    break

        # ----------------------------------------------------
        # Completely separate Revive engine.
        # ----------------------------------------------------

        revive_engine = ROIPortfolioEngine(
            policy_engine=self.policy_engine
        )

        revive_decisions = (
            revive_engine.run_portfolio(
                cases
            )
        )

        revive_metrics = (
            revive_engine.calculate_metrics(
                decisions=revive_decisions,
                cases=cases,
            )
        )

        return StrategyComparison(
            naive_attempts=(
                naive_attempts
            ),

            naive_cost=(
                naive_cost
            ),

            naive_recovered_amount=(
                naive_recovered
            ),

            revive_attempts=(
                revive_metrics
                .pursued_attempts
            ),

            revive_cost=(
                revive_metrics
                .recovery_cost
            ),

            revive_recovered_amount=(
                revive_metrics
                .recovered_amount
            ),

            cost_savings=(
                naive_cost
                - revive_metrics.recovery_cost
            ),

            additional_cost=(
                revive_metrics.recovery_cost
                - naive_cost
            ),

            additional_recovery=(
                revive_metrics.recovered_amount
                - naive_recovered
            ),
        )


# ============================================================
# Formatting
# ============================================================

def rupees(
    value: float,
) -> str:
    return f"₹{value:,.2f}"


# ============================================================
# Self-Test Helpers
# ============================================================

def print_checks(
    result: PolicyCheckResult,
) -> None:

    for check in result.checks:

        symbol = (
            "✓"
            if check.passed
            else "✗"
        )

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )


# ============================================================
# Module Entry
# ============================================================

if __name__ == "__main__":

    print("=" * 72)
    print("REVIVE — ROI PORTFOLIO ENGINE")
    print("=" * 72)
    print()
    print("ROI engine module loaded successfully.")
    print("Run the dedicated ROI tests for full validation.")