"""
Revive - End-to-End Recovery Pipeline

Integration layer for the completed Revive modules.

Architecture:

    DATA
      ↓
    DIAGNOSIS
      ↓
    PSR GUARDIAN
      ↓
    STRATEGY
      ↓
    POLICY GATE
      ↓
    ROI / EXPECTED VALUE
      ↓
    RECOVERY OUTCOME
      ↓
    A2A SETTLEMENT (eligible B2B cases)
      ↓
    RECOVERY LEDGER
      ↓
    UNIFIED BATCH RESULT


WHAT-IF SIMULATION SUPPORT:

    The pipeline can optionally receive an in-memory policy
    configuration.

    Normal execution:
        RevivePipeline()
              ↓
        policy.yaml

    Simulation execution:
        RevivePipeline(policy_override=temporary_policy)
              ↓
        temporary in-memory policy


IMPORTANT:

- Existing modules own their business rules.
- The pipeline does not duplicate policy, ROI or A2A rules.
- ROIPortfolioEngine owns all ROI attempt ledger writes.
- The pipeline never writes duplicate ledger events.
- One input case produces exactly one terminal PipelineCaseResult.
- Multiple ROI attempts remain available through the ledger.
- Policy overrides are never written to policy.yaml.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from app.diagnosis.classifier import (
    diagnose_case,
    load_cases,
)

from app.psr_guardian.guardian import (
    detect_alerts,
)

from app.core.policy import (
    PolicyEngine,
)

from app.orchestrator.orchestrator import (
    RecoveryOrchestrator,
)

from app.roi_engine.roi import (
    DECISION_PURSUED,
    DECISION_STOPPED,
    OUTCOME_NOT_RECOVERED,
    OUTCOME_RECOVERED,
    ROIDecision,
    ROIPortfolioEngine,
)

from app.a2a_settlement.settlement import (
    A2ASettlementEngine,
)

from app.recovery_ledger.ledger import (
    RecoveryLedger,
)

from app.mandate_sequencer.sequencer import (
    MandateRetrySequencer,
    load_mandate_retry_config,
)

from app.customers.directory import customer_directory


# ============================================================
# Constants
# ============================================================

EXPECTED_CASE_COUNT = 105

EPSILON = 0.000001

A2A_OUTCOME_SETTLED = "SETTLED"


# ============================================================
# Unified Case Result
# ============================================================

@dataclass(frozen=True)
class PipelineCaseResult:
    """Terminal case-level representation."""

    case_id: str
    surface: str
    customer_id: str
    amount: float

    root_cause: str
    action: str
    channel: str

    roi_decision: str
    roi_attempt_number: int
    roi_probability: float
    expected_recovery: float
    expected_value: float
    action_cost: float

    outcome: str
    recovered_amount: float

    policy_allowed: bool
    policy_blocking_reasons: tuple[str, ...]

    a2a_eligible: bool
    a2a_outcome: str | None

    # --------------------------------------------------------
    # Customer / payment identity.
    #
    # This is the terminal, dashboard-facing case representation
    # consumed by dashboard_api._find_pipeline_case() — which is
    # in turn what POST /api/promises, the retry-link path, and
    # the A2A settlement link path all use to resolve "who is this
    # case for" and "which payment does it map to". Without these
    # fields, every one of those lookups silently falls back to
    # None/"Revive Customer" even when the source case (cases.json
    # or a live Razorpay-webhook case) actually has the data,
    # because dataclasses.asdict() only ever emits declared
    # fields. Defaulted so this is purely additive for any
    # existing caller that constructs a PipelineCaseResult
    # positionally without them.
    # --------------------------------------------------------

    customer_name: str = ""
    customer_email: str | None = None
    invoice_id: str | None = None
    razorpay_payment_id: str | None = None

    # --------------------------------------------------------
    # Mandate retry sequencer.
    #
    # Populated only for `mandate_debit_failed` subscription
    # cases. Holds the compliant, policy-bounded retry schedule
    # (or escalation) proposed by app/mandate_sequencer, as a
    # plain dict so it survives dataclasses.asdict() for the
    # dashboard / audit trail. None for every other case —
    # additive and defaulted, same pattern as the fields above.
    # --------------------------------------------------------

    mandate_retry_plan: dict[str, Any] | None = None


# ============================================================
# Pipeline Metrics
# ============================================================

@dataclass(frozen=True)
class PipelineMetrics:
    """Batch-level metrics."""

    total_cases: int

    addressable_revenue: float

    pursued_cases: int
    stopped_cases: int

    pursued_attempts: int
    stopped_attempts: int

    recovered_cases: int
    recovered_revenue: float
    unrecovered_revenue: float

    recovery_cost: float
    net_recovered_value: float

    recovery_rate: float
    cost_per_rupee_recovered: float

    psr_alerts: int

    a2a_eligible_cases: int
    a2a_settled_cases: int

    ledger_events: int

    # --------------------------------------------------------
    # Naive-vs-Revive comparison
    # --------------------------------------------------------

    naive_comparison: dict[str, Any] | None = None


# ============================================================
# Pipeline Result
# ============================================================

@dataclass(frozen=True)
class PipelineResult:
    """Complete result of one pipeline execution."""

    cases: tuple[PipelineCaseResult, ...]

    metrics: PipelineMetrics

    psr_alerts: tuple[Any, ...]

    a2a_results: tuple[Any, ...]

    ledger: tuple[Any, ...]


# ============================================================
# Pipeline
# ============================================================

class RevivePipeline:
    """
    Integration layer connecting the completed Revive modules.

    The pipeline is intentionally thin.

    It coordinates modules rather than redefining their
    business logic.

    Parameters
    ----------
    policy_override:
        Optional in-memory policy dictionary.

        When None:
            PolicyEngine loads policy.yaml.

        When provided:
            PolicyEngine uses the supplied dictionary.

        The pipeline NEVER writes the override to disk.
    """

    def __init__(
        self,
        policy_override: dict[str, Any] | None = None,
    ) -> None:

        # ----------------------------------------------------
        # Store policy override.
        #
        # This remains in memory only.
        # ----------------------------------------------------

        self.policy_override = policy_override

        self._initialize_components()

    # ========================================================
    # Component initialization
    # ========================================================

    def _initialize_components(self) -> None:
        """
        Create fresh state for a new pipeline run.

        A supplied policy override is passed directly to the
        deterministic PolicyEngine.

        No policy file is modified.
        """

        if self.policy_override is not None:

            self.policy_engine = PolicyEngine(
                policy=self.policy_override
            )

        else:

            self.policy_engine = PolicyEngine()

        self.orchestrator = RecoveryOrchestrator(
            policy_engine=self.policy_engine
        )

        # ----------------------------------------------------
        # One authoritative ledger for the complete ROI run.
        # ----------------------------------------------------

        self.ledger = RecoveryLedger()

        self.roi_engine = ROIPortfolioEngine(
            policy_engine=self.policy_engine,
            ledger=self.ledger,
        )

        self.a2a_engine = A2ASettlementEngine(
            policy_engine=self.policy_engine
        )

        # ----------------------------------------------------
        # Mandate retry sequencer.
        #
        # Reads its config off the SAME resolved policy dict as
        # every other engine, so a what-if `policy_override` that
        # changes `mandate_retry` rules is honored here too.
        # ----------------------------------------------------

        self.mandate_sequencer = MandateRetrySequencer(
            config=load_mandate_retry_config(
                self.policy_engine.policy
            )
        )

    # ========================================================
    # Reset
    # ========================================================

    def reset(self) -> None:
        """
        Reset all stateful components.

        The existing policy override, if any, is preserved.
        """

        self._initialize_components()

    # ========================================================
    # PSR Guardian
    # ========================================================

    def run_psr_guardian(
        self,
        cases: list[dict[str, Any]],
    ) -> list[Any]:
        """Delegate systemic payment-risk detection."""

        return detect_alerts(cases)

    # ========================================================
    # A2A Settlement
    # ========================================================

    def run_a2a(
        self,
        cases: list[dict[str, Any]],
    ) -> list[Any]:
        """
        Run A2A settlement only for eligible B2B cases.

        Eligibility is determined by the dataset contract:

            surface == b2b_receivable
            has_ap_agent == True

        The actual settlement rules remain inside
        A2ASettlementEngine.
        """

        results: list[Any] = []

        for case in cases:

            if case.get(
                "surface"
            ) != "b2b_receivable":

                continue

            if not bool(
                case.get(
                    "has_ap_agent",
                    False,
                )
            ):

                continue

            result = self.a2a_engine.negotiate(
                case
            )

            results.append(
                result
            )

        return results

    # ========================================================
    # ROI grouping
    # ========================================================

    @staticmethod
    def group_roi_decisions_by_case(
        roi_decisions: list[ROIDecision],
    ) -> dict[str, list[ROIDecision]]:
        """
        Group ROI attempts by case.

        A case can have attempts #1, #2 and #3.

        Therefore positional zip() against the input cases
        is unsafe.
        """

        grouped: dict[
            str,
            list[ROIDecision],
        ] = {}

        for decision in roi_decisions:

            if not decision.case_id:

                raise ValueError(
                    "ROI decision contains an empty case_id."
                )

            grouped.setdefault(
                decision.case_id,
                [],
            ).append(
                decision
            )

        for case_id, history in grouped.items():

            history.sort(
                key=lambda item:
                item.attempt_number
            )

            if history[0].attempt_number != 1:

                raise AssertionError(
                    f"ROI history for {case_id} "
                    "must start at attempt #1."
                )

            for index in range(
                1,
                len(history),
            ):

                previous = history[
                    index - 1
                ]

                current = history[
                    index
                ]

                if (
                    current.attempt_number
                    != previous.attempt_number + 1
                ):

                    raise AssertionError(
                        f"Non-sequential ROI attempts "
                        f"for {case_id}."
                    )

        return grouped

    # ========================================================
    # Terminal decision
    # ========================================================

    @staticmethod
    def select_terminal_decision(
        history: list[ROIDecision],
    ) -> ROIDecision:
        """
        Select the case's terminal ROI decision.

        Priority:

            1. recovered attempt
            2. explicit STOP
            3. final attempt
        """

        if not history:

            raise ValueError(
                "Cannot select terminal decision "
                "from empty history."
            )

        history = sorted(
            history,
            key=lambda item:
            item.attempt_number,
        )

        recovered = [
            item
            for item in history
            if item.outcome
            == OUTCOME_RECOVERED
        ]

        if recovered:

            return max(
                recovered,
                key=lambda item:
                item.attempt_number,
            )

        stopped = [
            item
            for item in history
            if item.decision
            == DECISION_STOPPED
        ]

        if stopped:

            return max(
                stopped,
                key=lambda item:
                item.attempt_number,
            )

        return history[-1]

    # ========================================================
    # Ledger grouping
    # ========================================================

    def group_ledger_events_by_case(
        self,
    ) -> dict[str, list[Any]]:
        """Group authoritative ledger events by case."""

        grouped: dict[
            str,
            list[Any],
        ] = {}

        for event in self.ledger.all_events():

            if not event.case_id:

                raise AssertionError(
                    "Ledger event contains an empty case_id."
                )

            grouped.setdefault(
                event.case_id,
                [],
            ).append(
                event
            )

        for history in grouped.values():

            history.sort(
                key=lambda event: (
                    event.attempt_number,
                    event.timestamp,
                )
            )

        return grouped

    # ========================================================
    # Ledger validation
    # ========================================================

    def validate_ledger(
        self,
        cases: list[dict[str, Any]],
        roi_decisions: list[ROIDecision],
    ) -> None:
        """
        Prove that the authoritative ledger exactly represents
        ROI attempt history.
        """

        expected = (
            self.group_roi_decisions_by_case(
                roi_decisions
            )
        )

        actual = (
            self.group_ledger_events_by_case()
        )

        expected_ids = set(
            expected
        )

        actual_ids = set(
            actual
        )

        if expected_ids != actual_ids:

            raise AssertionError(
                "Ledger/ROI case coverage mismatch. "
                f"Missing: "
                f"{sorted(expected_ids - actual_ids)}; "
                f"Unexpected: "
                f"{sorted(actual_ids - expected_ids)}."
            )

        for case_id in expected_ids:

            roi_history = expected[
                case_id
            ]

            ledger_history = actual[
                case_id
            ]

            if len(
                roi_history
            ) != len(
                ledger_history
            ):

                raise AssertionError(
                    f"Ledger event count mismatch "
                    f"for {case_id}: "
                    f"ROI={len(roi_history)}, "
                    f"ledger={len(ledger_history)}."
                )

            for (
                roi_decision,
                ledger_event,
            ) in zip(
                roi_history,
                ledger_history,
            ):

                if (
                    roi_decision.attempt_number
                    != ledger_event.attempt_number
                ):

                    raise AssertionError(
                        f"Attempt mismatch "
                        f"for {case_id}."
                    )

                if (
                    roi_decision.decision
                    != ledger_event.decision
                ):

                    raise AssertionError(
                        f"Decision mismatch "
                        f"for {case_id}, "
                        f"attempt #"
                        f"{roi_decision.attempt_number}."
                    )

                if (
                    roi_decision.outcome
                    != ledger_event.outcome
                ):

                    raise AssertionError(
                        f"Outcome mismatch "
                        f"for {case_id}, "
                        f"attempt #"
                        f"{roi_decision.attempt_number}."
                    )

                if abs(
                    roi_decision
                    .explanation
                    .expected_value
                    - float(
                        ledger_event
                        .expected_value
                    )
                ) > EPSILON:

                    raise AssertionError(
                        f"EV mismatch "
                        f"for {case_id}, "
                        f"attempt #"
                        f"{roi_decision.attempt_number}."
                    )

    # ========================================================
    # Build unified case results
    # ========================================================

    def build_case_results(
        self,
        cases: list[dict[str, Any]],
        roi_decisions: list[ROIDecision],
        a2a_results: list[Any],
    ) -> list[PipelineCaseResult]:
        """
        Combine terminal ROI state, diagnosis, policy evidence
        and A2A result into one record per case.
        """

        roi_by_case = (
            self.group_roi_decisions_by_case(
                roi_decisions
            )
        )

        a2a_by_case = {
            result.case_id: result
            for result in a2a_results
        }

        results: list[
            PipelineCaseResult
        ] = []

        for case in cases:

            case_id = case[
                "case_id"
            ]

            history = roi_by_case.get(
                case_id
            )

            if not history:

                raise AssertionError(
                    f"ROI engine produced no decision "
                    f"for {case_id}."
                )

            terminal = (
                self.select_terminal_decision(
                    history
                )
            )

            # ------------------------------------------------
            # Diagnosis is retained as part of the integration
            # evidence. The terminal ROI decision remains the
            # authoritative source for the final root cause.
            # ------------------------------------------------

            diagnosis = diagnose_case(
                case
            )

            # ------------------------------------------------
            # Ask the same policy engine for the terminal action.
            #
            # This gives the unified result actual policy evidence
            # rather than inferring policy from the ROI decision.
            # ------------------------------------------------

            policy_result = (
                self.roi_engine.check_policy(
                    case=case,
                    action=terminal.action,
                    attempt_number=terminal.attempt_number,
                )
            )

            amount = float(
                case["amount"]
            )

            probability = float(
                terminal
                .explanation
                .final_success_probability
            )

            expected_recovery = float(
                terminal
                .explanation
                .expected_recovery
            )

            expected_value = float(
                terminal
                .explanation
                .expected_value
            )

            action_cost = float(
                terminal
                .explanation
                .action_cost
            )

            recovered_amount = float(
                terminal
                .recovered_amount
            )

            # ------------------------------------------------
            # Financial invariants
            # ------------------------------------------------

            assert 0.0 <= probability <= 1.0

            assert (
                0.0
                <= expected_recovery
                <= amount
            )

            assert action_cost >= 0.0

            assert (
                0.0
                <= recovered_amount
                <= amount
            )

            assert abs(
                expected_value
                - (
                    expected_recovery
                    - action_cost
                )
            ) < EPSILON

            # ------------------------------------------------
            # Critical safety invariant
            #
            # If policy blocks the terminal automated action,
            # ROI must not claim a recovered execution.
            #
            # Human escalation is intentionally allowed by policy.
            # ------------------------------------------------

            if (
                not policy_result.allowed
                and terminal.outcome
                == OUTCOME_RECOVERED
            ):

                raise AssertionError(
                    f"Unsafe state for {case_id}: "
                    "policy blocked the terminal action "
                    "but ROI reported RECOVERED."
                )

            a2a_result = (
                a2a_by_case.get(
                    case_id
                )
            )

            # ------------------------------------------------
            # A2A authority override
            #
            # For cases that went through agent-to-agent
            # settlement, the negotiation's verdict is the
            # actual recovery mechanism — not the ROI engine's
            # independent probability model, which was computed
            # before A2A ran and has no knowledge of whether the
            # negotiation actually succeeded.
            #
            # Without this override, a REJECTED or BLOCKED
            # negotiation could still be reported as RECOVERED
            # for the full amount, because the ROI-only
            # `terminal.outcome` and `recovered_amount` above are
            # blind to the A2A result computed in STEP 4.
            #
            # SETTLED  -> RECOVERED, for the negotiated final_amount
            # REJECTED / BLOCKED -> NOT_RECOVERED, ₹0
            # ------------------------------------------------

            final_outcome = terminal.outcome
            final_recovered_amount = recovered_amount

            if a2a_result is not None:

                if a2a_result.outcome == "SETTLED":
                    final_outcome = OUTCOME_RECOVERED
                    final_recovered_amount = float(
                        a2a_result.final_amount
                    )
                else:
                    final_outcome = OUTCOME_NOT_RECOVERED
                    final_recovered_amount = 0.0

                assert (
                    0.0
                    <= final_recovered_amount
                    <= amount
                ), (
                    f"Unsafe state for {case_id}: A2A "
                    f"final_amount out of bounds."
                )

                if (
                    a2a_result.outcome != "SETTLED"
                    and final_recovered_amount != 0.0
                ):

                    raise AssertionError(
                        f"Unsafe state for {case_id}: A2A "
                        f"outcome is '{a2a_result.outcome}' but "
                        f"recovered_amount is nonzero."
                    )

            directory_entry = customer_directory.get(
                case.get("customer_id")
            )

            # ------------------------------------------------
            # Mandate retry sequencer
            #
            # Only applicable to mandate_debit_failed subscription
            # cases. Built from the SAME terminal state the rest
            # of this result uses (root cause, attempt number),
            # so the plan always describes what actually happened
            # in this run — not a separate, possibly-inconsistent
            # simulation.
            # ------------------------------------------------

            mandate_retry_plan: dict[str, Any] | None = None

            if terminal.root_cause == "mandate_debit_failed":

                prior_attempts_this_cycle = max(
                    0,
                    terminal.attempt_number - 1,
                )

                plan = self.mandate_sequencer.build_plan(
                    case=case,
                    prior_attempts_this_cycle=(
                        prior_attempts_this_cycle
                    ),
                )

                mandate_retry_plan = plan.to_dict()

            results.append(
                PipelineCaseResult(

                    case_id=case_id,

                    surface=str(
                        case["surface"]
                    ),

                    customer_id=str(
                        case["customer_id"]
                    ),

                    amount=amount,

                    root_cause=(
                        terminal.root_cause
                    ),

                    action=terminal.action,

                    channel=terminal.channel,

                    roi_decision=(
                        terminal.decision
                    ),

                    roi_attempt_number=(
                        terminal.attempt_number
                    ),

                    roi_probability=(
                        probability
                    ),

                    expected_recovery=(
                        expected_recovery
                    ),

                    expected_value=(
                        expected_value
                    ),

                    action_cost=(
                        action_cost
                    ),

                    outcome=(
                        final_outcome
                    ),

                    recovered_amount=(
                        final_recovered_amount
                    ),

                    policy_allowed=(
                        policy_result.allowed
                    ),

                    policy_blocking_reasons=tuple(
                        policy_result
                        .blocking_reasons
                    ),

                    a2a_eligible=(
                        a2a_result is not None
                    ),

                    a2a_outcome=(
                        getattr(
                            a2a_result,
                            "outcome",
                            None,
                        )
                        if a2a_result is not None
                        else None
                    ),

                    customer_name=str(
                        case.get("customer_name")
                        or (directory_entry.get("name") if directory_entry else None)
                        or ""
                    ),

                    customer_email=(
                        str(case.get("customer_email")).strip()
                        if case.get("customer_email") is not None
                        else (
                            directory_entry.get("email")
                            if directory_entry
                            else None
                        )
                    ),

                    invoice_id=(
                        str(case.get("invoice_id"))
                        if case.get("invoice_id") is not None
                        else None
                    ),

                    razorpay_payment_id=(
                        str(case.get("razorpay_payment_id"))
                        if case.get("razorpay_payment_id") is not None
                        else None
                    ),

                    mandate_retry_plan=mandate_retry_plan,
                )
            )

        return results

    # ========================================================
    # Metrics
    # ========================================================

    def calculate_metrics(
        self,
        case_results: list[PipelineCaseResult],
        psr_alerts: list[Any],
        a2a_results: list[Any],
    ) -> PipelineMetrics:
        """
        Calculate case-level and attempt-level metrics.
        """

        total_cases = len(
            case_results
        )

        addressable = sum(
            result.amount
            for result in case_results
        )

        pursued_cases = sum(
            1
            for result in case_results
            if result.roi_decision
            == DECISION_PURSUED
        )

        stopped_cases = sum(
            1
            for result in case_results
            if result.roi_decision
            == DECISION_STOPPED
        )

        ledger_events = (
            self.ledger.all_events()
        )

        pursued_attempts = sum(
            1
            for event in ledger_events
            if event.decision
            == DECISION_PURSUED
        )

        stopped_attempts = sum(
            1
            for event in ledger_events
            if event.decision
            == DECISION_STOPPED
        )

        recovered_cases = sum(
            1
            for result in case_results
            if result.outcome
            == OUTCOME_RECOVERED
        )

        recovered_revenue = sum(
            result.recovered_amount
            for result in case_results
        )

        unrecovered = max(
            0.0,
            addressable
            - recovered_revenue,
        )

        recovery_cost = sum(
            float(
                event.action_cost
            )
            for event in ledger_events
            if event.decision
            == DECISION_PURSUED
        )

        net_value = (
            recovered_revenue
            - recovery_cost
        )

        recovery_rate = (
            recovered_revenue
            / addressable
            if addressable > 0
            else 0.0
        )

        cost_per_rupee = (
            recovery_cost
            / recovered_revenue
            if recovered_revenue > 0
            else 0.0
        )

        a2a_eligible = len(
            a2a_results
        )

        a2a_settled = sum(
            1
            for result in a2a_results
            if getattr(
                result,
                "outcome",
                None,
            )
            == A2A_OUTCOME_SETTLED
        )

        return PipelineMetrics(

            total_cases=(
                total_cases
            ),

            addressable_revenue=(
                addressable
            ),

            pursued_cases=(
                pursued_cases
            ),

            stopped_cases=(
                stopped_cases
            ),

            pursued_attempts=(
                pursued_attempts
            ),

            stopped_attempts=(
                stopped_attempts
            ),

            recovered_cases=(
                recovered_cases
            ),

            recovered_revenue=(
                recovered_revenue
            ),

            unrecovered_revenue=(
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

            psr_alerts=len(
                psr_alerts
            ),

            a2a_eligible_cases=(
                a2a_eligible
            ),

            a2a_settled_cases=(
                a2a_settled
            ),

            ledger_events=len(
                ledger_events
            ),
        )

    # ========================================================
    # Full pipeline
    # ========================================================

    def run(
        self,
        cases: list[dict[str, Any]],
    ) -> PipelineResult:
        """
        Execute one completely fresh end-to-end batch.
        """

        self.reset()

        if not cases:

            raise ValueError(
                "Revive pipeline received "
                "an empty case list."
            )

        # ----------------------------------------------------
        # Input validation
        # ----------------------------------------------------

        case_ids = [
            case.get(
                "case_id"
            )
            for case in cases
        ]

        if any(
            not case_id
            for case_id in case_ids
        ):

            raise ValueError(
                "Every case must contain "
                "a non-empty case_id."
            )

        if len(
            case_ids
        ) != len(
            set(case_ids)
        ):

            raise ValueError(
                "Duplicate case_id values detected."
            )

        required_fields = {
            "case_id",
            "surface",
            "customer_id",
            "amount",
        }

        for case in cases:

            missing = sorted(
                field
                for field in required_fields
                if field not in case
            )

            if missing:

                raise ValueError(
                    f"Case "
                    f"{case.get('case_id', '<unknown>')} "
                    f"is missing required fields: "
                    f"{', '.join(missing)}"
                )

            try:

                amount = float(
                    case["amount"]
                )

            except (
                TypeError,
                ValueError,
            ):

                raise ValueError(
                    f"Case {case['case_id']} "
                    "has an invalid amount."
                )

            if amount < 0:

                raise ValueError(
                    f"Case {case['case_id']} "
                    "has a negative amount."
                )

        expected_case_ids = set(
            case_ids
        )

        # ----------------------------------------------------
        # STEP 1 — PSR Guardian
        # ----------------------------------------------------

        psr_alerts = (
            self.run_psr_guardian(
                cases
            )
        )

        # ----------------------------------------------------
        # STEP 2 — ROI
        #
        # ROI engine itself performs:
        #
        # diagnosis
        #     →
        # strategy
        #     →
        # policy
        #     →
        # EV
        #     →
        # outcome
        #     →
        # ledger
        # ----------------------------------------------------

        roi_decisions = (
            self.roi_engine.run_portfolio(
                cases
            )
        )

        roi_by_case = (
            self.group_roi_decisions_by_case(
                roi_decisions
            )
        )

        if set(
            roi_by_case
        ) != expected_case_ids:

            raise AssertionError(
                "ROI case coverage does not "
                "match input cases."
            )

        # ----------------------------------------------------
        # STEP 3 — Ledger
        # ----------------------------------------------------

        self.validate_ledger(
            cases=cases,
            roi_decisions=roi_decisions,
        )

        # ----------------------------------------------------
        # STEP 4 — A2A
        # ----------------------------------------------------

        a2a_results = (
            self.run_a2a(
                cases
            )
        )

        # ----------------------------------------------------
        # STEP 5 — Unified case results
        # ----------------------------------------------------

        case_results = (
            self.build_case_results(
                cases=cases,
                roi_decisions=roi_decisions,
                a2a_results=a2a_results,
            )
        )

        if len(
            case_results
        ) != len(
            cases
        ):

            raise AssertionError(
                "Pipeline did not produce "
                "exactly one result per case."
            )

        result_ids = {
            result.case_id
            for result in case_results
        }

        if result_ids != expected_case_ids:

            raise AssertionError(
                "Unified case result IDs do not "
                "match input case IDs."
            )

        # ----------------------------------------------------
        # STEP 6 — Metrics
        # ----------------------------------------------------

        metrics = (
            self.calculate_metrics(
                case_results=case_results,
                psr_alerts=psr_alerts,
                a2a_results=a2a_results,
            )
        )

        # ----------------------------------------------------
        # STEP 6b — Naive-vs-Revive comparison
        #
        # calculate_naive_baseline() creates a separate ROI
        # engine and does not contaminate this pipeline ledger.
        # ----------------------------------------------------

        comparison = (
            self.roi_engine
            .calculate_naive_baseline(
                cases
            )
        )

        naive_comparison = asdict(
            comparison
        )

        # IMPORTANT: calculate_naive_baseline() uses its own
        # separate ROI engine with no knowledge of A2A settlement.
        # Left uncorrected, this card would show a different
        # "Revive recovers" number than the real headline metric.
        # Overriding with the true, A2A-aware totals from `metrics`
        # keeps every number on the dashboard mutually consistent.

        naive_comparison["revive_recovered_amount"] = (
            metrics.recovered_revenue
        )

        naive_comparison["revive_cost"] = (
            metrics.recovery_cost
        )

        naive_comparison["additional_recovery"] = (
            metrics.recovered_revenue
            - naive_comparison["naive_recovered_amount"]
        )

        naive_comparison["additional_cost"] = (
            metrics.recovery_cost
            - naive_comparison["naive_cost"]
        )

        naive_comparison["cost_savings"] = (
            naive_comparison["naive_cost"]
            - metrics.recovery_cost
        )

        if naive_comparison["additional_cost"] > 0:
            naive_comparison["summary"] = (
                f"Revive recovered an additional "
                f"₹{naive_comparison['additional_recovery']:,.2f} "
                f"for ₹{naive_comparison['additional_cost']:,.2f} "
                f"more in recovery cost than the naive strategy."
            )
        else:
            naive_comparison["summary"] = (
                f"Revive recovered an additional "
                f"₹{naive_comparison['additional_recovery']:,.2f} "
                f"while also costing "
                f"₹{abs(naive_comparison['additional_cost']):,.2f} "
                f"less than the naive strategy."
            )

        metrics = replace(
            metrics,
            naive_comparison=(
                naive_comparison
            ),
        )

        # ----------------------------------------------------
        # STEP 7 — Final ledger snapshot
        # ----------------------------------------------------

        ledger_snapshot = tuple(
            self.ledger.all_events()
        )

        return PipelineResult(

            cases=tuple(
                case_results
            ),

            metrics=metrics,

            psr_alerts=tuple(
                psr_alerts
            ),

            a2a_results=tuple(
                a2a_results
            ),

            ledger=ledger_snapshot,
        )


# ============================================================
# Serialization helper
# ============================================================

def pipeline_to_dict(
    result: PipelineResult,
) -> dict[str, Any]:
    """
    Convert the immutable pipeline result into a JSON-friendly
    dictionary.

    FastAPI can use this directly with JSONResponse or return
    the dictionary from an endpoint.
    """

    return {

        "success": True,

        "summary": asdict(
            result.metrics
        ),

        "metrics": asdict(
            result.metrics
        ),

        "psr_alerts": [

            (
                asdict(alert)
                if hasattr(
                    alert,
                    "__dataclass_fields__"
                )
                else alert
            )

            for alert in result.psr_alerts
        ],

        "a2a_settlements": [

            (
                asdict(item)
                if hasattr(
                    item,
                    "__dataclass_fields__"
                )
                else item
            )

            for item in result.a2a_results
        ],

        "cases": [

            asdict(case)

            for case in result.cases
        ],

        "ledger": [

            (
                asdict(event)
                if hasattr(
                    event,
                    "__dataclass_fields__"
                )
                else event
            )

            for event in result.ledger
        ],
    }


# ============================================================
# Formatting
# ============================================================

def rupees(
    value: float,
) -> str:

    return f"₹{value:,.2f}"


# ============================================================
# Self-test
# ============================================================

def main() -> None:

    print(
        "=" * 72
    )

    print(
        "REVIVE — END-TO-END RECOVERY PIPELINE"
    )

    print(
        "=" * 72
    )

    # --------------------------------------------------------
    # Dataset
    # --------------------------------------------------------

    cases = load_cases()

    print()

    print(
        f"Loaded cases: {len(cases)}"
    )

    assert (
        len(cases)
        == EXPECTED_CASE_COUNT
    )

    # --------------------------------------------------------
    # Run
    # --------------------------------------------------------

    pipeline = RevivePipeline()

    print()

    print(
        "Running complete pipeline..."
    )

    result = pipeline.run(
        cases
    )

    metrics = result.metrics

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()

    print(
        "Pipeline summary:"
    )

    print(
        f"  Cases processed:       "
        f"{metrics.total_cases}"
    )

    print(
        f"  Addressable revenue:   "
        f"{rupees(metrics.addressable_revenue)}"
    )

    print(
        f"  Pursued cases:         "
        f"{metrics.pursued_cases}"
    )

    print(
        f"  Stopped cases:         "
        f"{metrics.stopped_cases}"
    )

    print(
        f"  Pursued attempts:      "
        f"{metrics.pursued_attempts}"
    )

    print(
        f"  Stopped attempts:      "
        f"{metrics.stopped_attempts}"
    )

    print(
        f"  Recovered cases:       "
        f"{metrics.recovered_cases}"
    )

    print(
        f"  Recovered revenue:     "
        f"{rupees(metrics.recovered_revenue)}"
    )

    print(
        f"  Unrecovered revenue:   "
        f"{rupees(metrics.unrecovered_revenue)}"
    )

    print(
        f"  Recovery cost:         "
        f"{rupees(metrics.recovery_cost)}"
    )

    print(
        f"  Net recovered value:   "
        f"{rupees(metrics.net_recovered_value)}"
    )

    print(
        f"  Recovery rate:         "
        f"{metrics.recovery_rate:.2%}"
    )

    print(
        f"  Cost / ₹ recovered:    "
        f"{metrics.cost_per_rupee_recovered:.6f}"
    )

    print(
        f"  PSR alerts:            "
        f"{metrics.psr_alerts}"
    )

    print(
        f"  A2A eligible:          "
        f"{metrics.a2a_eligible_cases}"
    )

    print(
        f"  A2A settled:           "
        f"{metrics.a2a_settled_cases}"
    )

    print(
        f"  Ledger events:         "
        f"{metrics.ledger_events}"
    )

    # ========================================================
    # Integrity checks
    # ========================================================

    print()

    print(
        "Pipeline integrity checks:"
    )

    # --------------------------------------------------------
    # Dataset coverage
    # --------------------------------------------------------

    assert (
        metrics.total_cases
        == EXPECTED_CASE_COUNT
    )

    assert (
        len(result.cases)
        == EXPECTED_CASE_COUNT
    )

    print(
        "  ✓ All 105 cases produced unified results."
    )

    # --------------------------------------------------------
    # Case IDs
    # --------------------------------------------------------

    case_ids = {
        case.case_id
        for case in result.cases
    }

    assert (
        len(case_ids)
        == EXPECTED_CASE_COUNT
    )

    print(
        "  ✓ All case IDs are unique."
    )

    # --------------------------------------------------------
    # Terminal decision coverage
    # --------------------------------------------------------

    assert (
        metrics.pursued_cases
        + metrics.stopped_cases
        == EXPECTED_CASE_COUNT
    )

    print(
        "  ✓ Every case has a terminal "
        "PURSUE/STOP decision."
    )

    # --------------------------------------------------------
    # Revenue bounds
    # --------------------------------------------------------

    assert (
        metrics.addressable_revenue
        > 0
    )

    assert (
        0
        <= metrics.recovered_revenue
        <= metrics.addressable_revenue
    )

    assert (
        metrics.unrecovered_revenue
        >= 0
    )

    print(
        "  ✓ Revenue totals are financially bounded."
    )

    # --------------------------------------------------------
    # ROI consistency
    # --------------------------------------------------------

    for case in result.cases:

        assert (
            0
            <= case.roi_probability
            <= 1
        )

        assert (
            0
            <= case.expected_recovery
            <= case.amount
        )

        assert (
            case.action_cost
            >= 0
        )

        assert (
            0
            <= case.recovered_amount
            <= case.amount
        )

        calculated_ev = (
            case.expected_recovery
            - case.action_cost
        )

        assert abs(
            calculated_ev
            - case.expected_value
        ) < EPSILON

    print(
        "  ✓ ROI probability, recovery and "
        "EV values are consistent."
    )

    # --------------------------------------------------------
    # Policy safety
    # --------------------------------------------------------

    policy_blocked = [
        case
        for case in result.cases
        if not case.policy_allowed
    ]

    for case in policy_blocked:

        # A2A settlement has its own separate policy gate —
        # contact-hours restrictions govern human-channel contact
        # and don't apply to a machine-to-machine negotiation. So a
        # case can legitimately be policy_allowed=False (its
        # orchestrated WhatsApp/voice action is blocked) while
        # still being genuinely recovered via A2A settlement.

        if case.a2a_outcome == "SETTLED":

            assert (
                case.outcome
                == OUTCOME_RECOVERED
            )

            # NOTE: a SETTLED negotiation can legitimately close
            # at a discount (up to policy's max_discount_percent),
            # so recovered_amount is bounded by the original
            # amount, not necessarily equal to it. See
            # a2a_settlement/settlement.py's negotiate() —
            # OUTCOME_SETTLED sets final_amount to whatever the
            # payer agent actually accepted, which may be less
            # than the full invoice.
            assert (
                0.0
                < case.recovered_amount
                <= case.amount
            )

        else:

            assert (
                case.outcome
                != OUTCOME_RECOVERED
            )

            assert (
                case.recovered_amount
                == 0.0
            )

    a2a_recovered_despite_policy_block = [
        case
        for case in policy_blocked
        if case.a2a_outcome == "SETTLED"
    ]

    if a2a_recovered_despite_policy_block:

        print(
            f"  ✓ {len(policy_blocked)} policy-blocked cases: "
            f"{len(a2a_recovered_despite_policy_block)} still "
            f"recovered via A2A (machine-to-machine, not bound "
            f"by human contact-hours), the rest correctly show "
            f"zero recovery."
        )

    else:

        print(
            f"  ✓ {len(policy_blocked)} "
            "policy-blocked cases cannot recover revenue."
        )

    # --------------------------------------------------------
    # PSR
    # --------------------------------------------------------

    assert (
        metrics.psr_alerts
        >= 1
    )

    print(
        "  ✓ PSR Guardian produced systemic alerts."
    )

    # --------------------------------------------------------
    # A2A
    # --------------------------------------------------------

    # NOTE: this is a snapshot check against the current
    # deterministic dataset (seed 42, data/generate_data.py),
    # not a business rule. It was 11 before mandate_debit_failed
    # was added to the subscription root-cause pool — adding that
    # option shifts random.choice()'s rejection-sampling draws for
    # every case generated after it in the same RNG stream,
    # including the B2B cases that determine A2A eligibility. If
    # generate_data.py changes again, re-run it and update this
    # number to match, rather than assuming a regression.
    assert (
        metrics.a2a_eligible_cases
        == 10
    )

    print(
        "  ✓ A2A eligibility matches the "
        "10 eligible dataset cases."
    )

    # --------------------------------------------------------
    # Ledger
    # --------------------------------------------------------

    assert (
        metrics.ledger_events
        == len(result.ledger)
    )

    assert (
        metrics.ledger_events
        > 0
    )

    print(
        "  ✓ Authoritative recovery ledger is populated."
    )

    # --------------------------------------------------------
    # Ledger case coverage
    # --------------------------------------------------------

    ledger_case_ids = {
        event.case_id
        for event in result.ledger
    }

    assert (
        ledger_case_ids
        == case_ids
    )

    print(
        "  ✓ Every case is represented in the ledger."
    )

    # --------------------------------------------------------
    # Ledger decisions
    # --------------------------------------------------------

    assert all(
        event.decision
        in {
            DECISION_PURSUED,
            DECISION_STOPPED,
        }
        for event in result.ledger
    )

    print(
        "  ✓ Ledger decisions are valid."
    )

    # --------------------------------------------------------
    # Attempt ordering
    # --------------------------------------------------------

    ledger_by_case = (
        pipeline.group_ledger_events_by_case()
    )

    for (
        case_id,
        history,
    ) in ledger_by_case.items():

        assert (
            history[0]
            .attempt_number
            == 1
        )

        for index in range(
            1,
            len(history),
        ):

            assert (
                history[index]
                .attempt_number
                ==
                history[index - 1]
                .attempt_number
                + 1
            )

    print(
        "  ✓ Ledger attempt histories are sequential."
    )

    # --------------------------------------------------------
    # Unified result must match terminal ledger state
    # --------------------------------------------------------

    for unified in result.cases:

        history = ledger_by_case[
            unified.case_id
        ]

        terminal_event = max(
            history,
            key=lambda event:
            event.attempt_number,
        )

        assert (
            unified.roi_attempt_number
            == terminal_event.attempt_number
        )

        assert (
            unified.roi_decision
            == terminal_event.decision
        )

        # unified.outcome matches the raw ROI ledger terminal
        # outcome UNLESS this case was authoritatively overridden
        # by its A2A settlement result.

        if unified.a2a_outcome is None:

            assert (
                unified.outcome
                == terminal_event.outcome
            )

        else:

            expected_outcome = (
                OUTCOME_RECOVERED
                if unified.a2a_outcome == "SETTLED"
                else OUTCOME_NOT_RECOVERED
            )

            assert (
                unified.outcome
                == expected_outcome
            )

        assert abs(
            unified.expected_value
            - float(
                terminal_event
                .expected_value
            )
        ) < EPSILON

        assert abs(
            unified.action_cost
            - float(
                terminal_event
                .action_cost
            )
        ) < EPSILON

    print(
        "  ✓ Unified case results match "
        "terminal ledger state."
    )

    # --------------------------------------------------------
    # A2A result coverage
    # --------------------------------------------------------

    a2a_ids = {
        item.case_id
        for item in result.a2a_results
    }

    assert (
        len(a2a_ids)
        == metrics.a2a_eligible_cases
    )

    for case in result.cases:

        if case.a2a_eligible:

            assert (
                case.case_id
                in a2a_ids
            )

    print(
        "  ✓ A2A results map correctly "
        "to eligible cases."
    )

    # --------------------------------------------------------
    # JSON-friendly serialization
    # --------------------------------------------------------

    payload = pipeline_to_dict(
        result
    )

    assert (
        payload["success"]
        is True
    )

    assert (
        len(
            payload["cases"]
        )
        == EXPECTED_CASE_COUNT
    )

    assert (
        len(
            payload["ledger"]
        )
        == metrics.ledger_events
    )

    print(
        "  ✓ Complete pipeline result is JSON-serializable."
    )

    # --------------------------------------------------------
    # Reset / repeat-run safety
    # --------------------------------------------------------

    second_result = pipeline.run(
        cases
    )

    assert (
        len(
            second_result.ledger
        )
        ==
        len(
            result.ledger
        )
    )

    assert (
        second_result.metrics.total_cases
        ==
        result.metrics.total_cases
    )

    print(
        "  ✓ Re-running the same pipeline "
        "starts with fresh state."
    )

    # --------------------------------------------------------
    # Policy override self-test
    # --------------------------------------------------------
    #
    # This verifies that simulation-style policy injection
    # works without touching the normal policy file.
    # --------------------------------------------------------

    simulation_policy = {
        key: value
        for key, value
        in pipeline.policy_engine.policy.items()
    }

    # Nested structures must be copied separately for a real
    # simulation. The API uses deepcopy(), so this self-test
    # intentionally demonstrates the expected structure without
    # mutating the original pipeline policy.

    from copy import deepcopy

    simulation_policy = deepcopy(
        pipeline.policy_engine.policy
    )

    original_contact_limit = int(
        simulation_policy[
            "max_contact_attempts"
        ]
    )

    simulation_policy[
        "max_contact_attempts"
    ] = (
        original_contact_limit + 1
    )

    simulation_pipeline = RevivePipeline(
        policy_override=simulation_policy
    )

    simulation_result = (
        simulation_pipeline.run(
            cases
        )
    )

    assert (
        simulation_result.metrics.total_cases
        == EXPECTED_CASE_COUNT
    )

    assert (
        simulation_pipeline
        .policy_engine
        .policy[
            "max_contact_attempts"
        ]
        == original_contact_limit + 1
    )

    # --------------------------------------------------------
    # Confirm the original pipeline still uses the original
    # policy.
    # --------------------------------------------------------

    assert (
        pipeline.policy_engine
        .policy[
            "max_contact_attempts"
        ]
        == original_contact_limit
    )

    print(
        "  ✓ Temporary policy overrides work correctly."
    )

    print(
        "  ✓ Simulation policy does not mutate "
        "the original policy."
    )

    # --------------------------------------------------------
    # Sample cases
    # --------------------------------------------------------

    print()

    print(
        "Sample terminal decisions:"
    )

    for case in result.cases[:5]:

        print()

        print(
            f"  {case.case_id} | "
            f"{case.root_cause} | "
            f"{case.action} | "
            f"{case.roi_decision} | "
            f"{case.outcome}"
        )

        print(
            f"    P(success): "
            f"{case.roi_probability:.2%}"
        )

        print(
            f"    EV: "
            f"{rupees(case.expected_value)}"
        )

        print(
            f"    Policy: "
            f"{'ALLOWED' if case.policy_allowed else 'BLOCKED'}"
        )

    # --------------------------------------------------------
    # Final
    # --------------------------------------------------------

    print()

    print(
        "=" * 72
    )

    print(
        "REVIVE END-TO-END PIPELINE: PASSED"
    )

    print(
        "=" * 72
    )


if __name__ == "__main__":
    main()