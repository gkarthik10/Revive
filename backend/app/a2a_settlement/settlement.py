"""
Revive - Module 6
Agent-to-Agent Settlement Protocol

Demonstrates bounded agent-to-agent settlement negotiation
for B2B receivables where the payer has its own AP agent.

Architecture:

    Merchant Recovery Agent
              |
              v
       Settlement Request
              |
              v
        Payer AP Agent
              |
        accept / counter /
             reject
              |
              v
       Bounded negotiation
              |
              v
        Settlement outcome

Important:

    - Module 3 remains the general policy authority.
    - Module 6 does NOT modify PolicyEngine.
    - Negotiation rounds are explicitly bounded.
    - Merchant discount limits come from policy.yaml.
    - Payer agent has independent constraints.
    - Disputed invoices are never automatically negotiated.
    - Every round produces an auditable transcript.
    - Every proposal produces explicit policy evidence.
    - No external A2A service is required.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.core.policy import (
    CaseState,
    PolicyEngine,
)

from app.diagnosis.classifier import (
    load_cases,
)

import hashlib
import json
import logging
import os
from decimal import Decimal

logger = logging.getLogger(__name__)

from app.a2a_settlement.a2a_client import (
    A2AClientError,
    HttpA2APayerAgentClient,
    build_remote_payer_client,
)

# ============================================================
# Constants
# ============================================================

STATUS_ACCEPTED = "ACCEPTED"
STATUS_COUNTER_OFFER = "COUNTER_OFFER"
STATUS_REJECTED = "REJECTED"
STATUS_EXPIRED = "EXPIRED"
STATUS_BLOCKED = "BLOCKED"

OUTCOME_SETTLED = "SETTLED"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_BLOCKED = "BLOCKED"
OUTCOME_EXPIRED = "EXPIRED"


# ============================================================
# Data classes
# ============================================================

@dataclass(frozen=True)
class SettlementProposal:
    """
    JSON-like settlement proposal exchanged between agents.
    """

    invoice_id: str
    amount: float
    due_date: str
    proposed_terms: str
    authorization_proof: str
    expiry: str


@dataclass(frozen=True)
class PayerAgentConstraints:
    """
    Independent constraints controlled by the payer agent.

    These are intentionally separate from merchant policy.
    """

    maximum_payment_amount: float
    minimum_discount_percent: float
    max_rounds: int
    accepts_installments: bool
    installment_terms: str


@dataclass(frozen=True)
class NegotiationRound:
    """
    Complete audit record for one negotiation round.
    """

    round_number: int
    merchant_amount: float
    payer_amount: float
    discount_percent: float
    merchant_status: str
    payer_status: str
    message: str


@dataclass(frozen=True)
class PolicyEvidence:
    """
    Policy evidence for a settlement proposal.

    Every negotiation result must carry explicit checks.
    """

    allowed: bool
    checks: list[dict[str, Any]]
    blocking_reasons: list[str]


@dataclass(frozen=True)
class SettlementResult:
    """
    Complete result of one A2A negotiation.

    outcome:
        Existing Revive outcome used by the current pipeline.

    settlement_status:
        AGREED means agents agreed on terms.
        This does NOT mean money has been received.

    payment_status:
        Tracks actual payment confirmation separately.
    """

    case_id: str
    invoice_id: str
    eligible: bool
    outcome: str
    final_amount: float
    discount_percent: float
    rounds: int
    reason: str
    policy_evidence: PolicyEvidence
    transcript: list[NegotiationRound]

    settlement_status: str = "NOT_STARTED"
    payment_status: str = "NOT_STARTED"
    recovery_confirmed: bool = False

    agreement_id: str | None = None
    a2a_agent_id: str | None = None
    a2a_task_id: str | None = None
    a2a_context_id: str | None = None


# ============================================================
# A2A Settlement Engine
# ============================================================

class A2ASettlementEngine:
    """
    Bounded agent-to-agent settlement negotiation engine.

    Merchant side:
        constrained by Module 3 policy.

    Payer side:
        constrained by independent payer-agent rules.

    This is a demonstration protocol, not a production
    payment protocol.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        payer_agent_client: (
            HttpA2APayerAgentClient | None
        ) = None,
    ) -> None:

        self.policy_engine = (
            policy_engine
            if policy_engine is not None
            else PolicyEngine()
        )

        self.policy = self.policy_engine.policy

        self.max_rounds = int(
            self.policy[
                "max_negotiation_rounds"
            ]
        )

        self.max_discount = float(
            self.policy[
                "max_discount_percent"
            ]
        )

        # --------------------------------------------------------
        # Real A2A client.
        #
        # If A2A_PAYER_AGENT_CARD_URL exists, Revive uses the
        # remote payer agent.
        #
        # If it does not exist, the existing synthetic agent
        # remains available for offline benchmark/self-test.
        #
        # IMPORTANT: build_remote_payer_client() performs a live
        # network discovery call (HttpA2APayerAgentClient.__init__
        # -> _discover_agent()). If the configured payer-agent
        # endpoint is unreachable, misconfigured, or times out,
        # that must NOT take down pipeline construction (and with
        # it, nearly every dashboard endpoint). Fall back to the
        # offline synthetic agent instead, and record why.
        # --------------------------------------------------------

        self.a2a_client_error: str | None = None

        if payer_agent_client is not None:
            self.payer_agent_client = payer_agent_client

        else:
            try:
                self.payer_agent_client = (
                    build_remote_payer_client()
                )

            except A2AClientError as exc:
                logger.warning(
                    "A2A payer-agent discovery failed; "
                    "falling back to offline synthetic agent: %s",
                    exc,
                )
                self.payer_agent_client = None
                self.a2a_client_error = str(exc)

        self.a2a_mode = (
            "remote"
            if self.payer_agent_client is not None
            else "mock"
        )

    # ========================================================
    # Eligibility
    # ========================================================

    def is_eligible(
        self,
        case: dict[str, Any],
    ) -> tuple[bool, str]:

        if case.get("surface") != "b2b_receivable":

            return (
                False,
                (
                    "A2A settlement is only available "
                    "for B2B receivables."
                ),
            )

        if not case.get(
            "has_ap_agent",
            False,
        ):

            return (
                False,
                "Payer does not have an AP agent.",
            )

        if (
            case.get("root_cause_label")
            == "invoice_dispute"
        ):

            return (
                False,
                (
                    "Invoice is disputed; automated "
                    "settlement negotiation is prohibited."
                ),
            )

        return (
            True,
            (
                "B2B receivable has an authorized payer "
                "AP agent and is eligible for bounded "
                "A2A settlement."
            ),
        )

    # ========================================================
    # Payer constraints
    # ========================================================

    def build_payer_constraints(
        self,
        case: dict[str, Any],
    ) -> PayerAgentConstraints:

        amount = float(
            case["amount"]
        )

        root_cause = case.get(
            "root_cause_label",
            "",
        )

        # ----------------------------------------------------
        # Synthetic payer-agent behavior.
        #
        # This represents a second autonomous agent with
        # constraints independent from merchant policy.
        # ----------------------------------------------------

        if root_cause == "b2b_cashflow_delay":

            return PayerAgentConstraints(
                maximum_payment_amount=(
                    amount * 0.95
                ),
                minimum_discount_percent=5.0,
                max_rounds=3,
                accepts_installments=True,
                installment_terms=(
                    "50% immediately and 50% within 30 days."
                ),
            )

        if root_cause == "payment_approval_delay":

            return PayerAgentConstraints(
                maximum_payment_amount=amount,
                minimum_discount_percent=0.0,
                max_rounds=2,
                accepts_installments=True,
                installment_terms=(
                    "Payment requires internal approval "
                    "before settlement."
                ),
            )

        return PayerAgentConstraints(
            maximum_payment_amount=amount,
            minimum_discount_percent=0.0,
            max_rounds=2,
            accepts_installments=False,
            installment_terms=(
                "No installment plan available."
            ),
        )

    # ========================================================
    # Authorization
    # ========================================================

    def build_authorization_proof(
        self,
        case: dict[str, Any],
    ) -> str:

        return (
            "synthetic-ap-agent-authorization:"
            f"{case['customer_id']}:"
            f"{case['case_id']}"
        )

    # ========================================================
    # Proposal creation
    # ========================================================

    def create_proposal(
        self,
        case: dict[str, Any],
        amount: float,
        terms: str,
        expiry: str,
    ) -> SettlementProposal:

        return SettlementProposal(
            invoice_id=(
                f"INV-{case['case_id']}"
            ),
            amount=round(
                amount,
                2,
            ),
            due_date=str(
                case.get(
                    "due_date",
                    "",
                )
            ),
            proposed_terms=terms,
            authorization_proof=(
                self.build_authorization_proof(
                    case
                )
            ),
            expiry=expiry,
        )

    # ========================================================
    # Discount calculation
    # ========================================================

    @staticmethod
    def calculate_discount_percent(
        original_amount: float,
        proposed_amount: float,
    ) -> float:

        if original_amount <= 0:

            raise ValueError(
                "Original amount must be greater than zero."
            )

        discount = (
            (
                original_amount
                - proposed_amount
            )
            / original_amount
            * 100
        )

        return max(
            0.0,
            discount,
        )

    # ========================================================
    # Policy evidence conversion
    # ========================================================

    @staticmethod
    def _policy_check_to_dict(
        check: Any,
    ) -> dict[str, Any]:
        """
        Convert Module 3 policy-check objects into a stable
        JSON-friendly representation.
        """

        if isinstance(
            check,
            dict,
        ):

            return {
                "name": check.get(
                    "name",
                    "policy_check",
                ),
                "passed": bool(
                    check.get(
                        "passed",
                        False,
                    )
                ),
                "message": str(
                    check.get(
                        "message",
                        "",
                    )
                ),
            }

        if hasattr(
            check,
            "__dataclass_fields__",
        ):

            data = asdict(
                check
            )

            return {
                "name": data.get(
                    "name",
                    "policy_check",
                ),
                "passed": bool(
                    data.get(
                        "passed",
                        False,
                    )
                ),
                "message": str(
                    data.get(
                        "message",
                        "",
                    )
                ),
            }

        return {
            "name": getattr(
                check,
                "name",
                "policy_check",
            ),
            "passed": bool(
                getattr(
                    check,
                    "passed",
                    False,
                )
            ),
            "message": str(
                getattr(
                    check,
                    "message",
                    str(check),
                )
            ),
        }

    # ========================================================
    # Proposal validation
    # ========================================================

    def validate_proposal(
        self,
        case: dict[str, Any],
        round_number: int,
        amount: float,
        state: CaseState,
        now: datetime,
    ) -> PolicyEvidence:
        """
        Validate one A2A settlement proposal.

        Module 3 remains the authoritative policy engine.

        Module 6 adds:

            - negotiation round limit
            - discount limit

        IMPORTANT:

        PolicyEngine.check_action() receives only the arguments
        supported by Module 3:

            state
            action
            now

        Negotiation-specific checks are handled here.
        """

        original_amount = float(
            case["amount"]
        )

        discount_percent = (
            self.calculate_discount_percent(
                original_amount,
                amount,
            )
        )

        checks: list[
            dict[str, Any]
        ] = []

        blocking_reasons: list[str] = []

        # ====================================================
        # General Module 3 policy
        # ====================================================

        policy_result = (
            self.policy_engine.check_action(
                state=state,
                action="negotiate",
                now=now,
            )
        )

        for check in policy_result.checks:

            checks.append(
                self._policy_check_to_dict(
                    check
                )
            )

        blocking_reasons.extend(
            policy_result.blocking_reasons
        )

        # ====================================================
        # Negotiation round limit
        # ====================================================

        round_passed = (
            round_number
            <= self.max_rounds
        )

        checks.append(
            {
                "name": "negotiation_round_limit",
                "passed": round_passed,
                "message": (
                    f"Round {round_number} "
                    f"{'is within' if round_passed else 'exceeds'} "
                    f"merchant maximum of "
                    f"{self.max_rounds}."
                ),
            }
        )

        if not round_passed:

            blocking_reasons.append(
                (
                    f"Negotiation round {round_number} "
                    f"exceeds maximum allowed rounds "
                    f"of {self.max_rounds}."
                )
            )

        # ====================================================
        # Discount limit
        # ====================================================

        discount_passed = (
            discount_percent
            <= self.max_discount
            + 0.000001
        )

        checks.append(
            {
                "name": "discount_limit",
                "passed": discount_passed,
                "message": (
                    f"Requested discount "
                    f"{discount_percent:.2f}% "
                    f"{'is within' if discount_passed else 'exceeds'} "
                    f"merchant cap of "
                    f"{self.max_discount:.2f}%."
                ),
            }
        )

        if not discount_passed:

            blocking_reasons.append(
                (
                    f"Requested discount "
                    f"{discount_percent:.2f}% exceeds "
                    f"the {self.max_discount:.2f}% cap."
                )
            )

        # ====================================================
        # Guarantee evidence exists
        # ====================================================

        if not checks:

            checks.append(
                {
                    "name": "policy_evaluation",
                    "passed": True,
                    "message": (
                        "Proposal evaluated against "
                        "merchant settlement policy."
                    ),
                }
            )

        return PolicyEvidence(
            allowed=(
                len(blocking_reasons)
                == 0
            ),
            checks=checks,
            blocking_reasons=(
                blocking_reasons
            ),
        )

    def build_remote_settlement_payload(
        self,
        case: dict[str, Any],
        proposal: SettlementProposal,
        round_number: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """
        Build the business-level settlement contract exchanged
        through A2A.

        A2A handles agent communication.

        This Revive contract handles settlement semantics.
        """

        return {
            "contract_version": "revive.settlement.v1",

            "case_id": str(
                case["case_id"]
            ),

            "invoice_id": str(
                proposal.invoice_id
            ),

            "customer_id": str(
                case.get(
                    "customer_id",
                    "",
                )
            ),

            "original_amount": (
                f"{Decimal(str(case['amount'])):.2f}"
            ),

            "amount": (
                f"{Decimal(str(proposal.amount)):.2f}"
            ),

            "due_date": str(
                proposal.due_date
            ),

            "proposed_terms": str(
                proposal.proposed_terms
            ),

            "authorization_proof": str(
                proposal.authorization_proof
            ),

            "expiry": str(
                proposal.expiry
            ),

            "round": round_number,

            "idempotency_key": idempotency_key,
        }

    def build_idempotency_key(
        self,
        case: dict[str, Any],
        proposal: SettlementProposal,
        round_number: int,
    ) -> str:
        """
        Deterministic identifier for one exact proposal.

        Same case + invoice + round + amount produces the same key.
        """

        raw = (
            f"{case['case_id']}|"
            f"{proposal.invoice_id}|"
            f"{round_number}|"
            f"{Decimal(str(proposal.amount)):.2f}"
        )

        return hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

    # ========================================================
    # Payer agent
    # ========================================================

    def payer_agent_response(
        self,
        proposal: SettlementProposal,
        original_amount: float,
        constraints: PayerAgentConstraints,
        round_number: int,
        *,
        case: dict[str, Any] | None = None,
    ) -> tuple[
        str,
        float,
        str,
    ]:
        """
        Obtain a payer-agent decision.

        Remote mode:
            Communicates with an independent A2A payer agent.

        Mock mode:
            Uses the existing deterministic payer logic for
            offline tests.

        IMPORTANT:
            A remote failure is never interpreted as acceptance.
        """

        # ========================================================
        # REAL REMOTE A2A
        # ========================================================

        if self.payer_agent_client is not None:

            if case is None:

                raise A2AClientError(
                    "Case is required for remote A2A negotiation."
                )

            idempotency_key = (
                self.build_idempotency_key(
                    case=case,
                    proposal=proposal,
                    round_number=round_number,
                )
            )

            payload = (
                self.build_remote_settlement_payload(
                    case=case,
                    proposal=proposal,
                    round_number=round_number,
                    idempotency_key=idempotency_key,
                )
            )

            response = (
                self.payer_agent_client.send_settlement(
                    settlement_payload=payload,
                )
            )

            payer_amount = response.amount

            # ----------------------------------------------------
            # Never allow remote payer to exceed original invoice.
            # ----------------------------------------------------

            if payer_amount > Decimal(
                str(original_amount)
            ):

                raise A2AClientError(
                    "Payer agent returned an amount greater "
                    "than the original invoice."
                )

            if payer_amount < 0:

                raise A2AClientError(
                    "Payer agent returned a negative amount."
                )

            return (
                response.decision,
                float(payer_amount),
                (
                    response.message
                    or "Payer agent returned a valid A2A decision."
                ),
            )

        # ========================================================
        # EXISTING OFFLINE MOCK
        # ========================================================

        amount = float(
            proposal.amount
        )

        if (
            round_number
            > constraints.max_rounds
        ):

            return (
                STATUS_REJECTED,
                amount,
                (
                    "Payer AP agent rejected the request "
                    "because its maximum negotiation rounds "
                    "were exceeded."
                ),
            )

        if (
            amount
            <= constraints.maximum_payment_amount
        ):

            return (
                STATUS_ACCEPTED,
                amount,
                (
                    "Payer AP agent accepts the proposed "
                    "settlement amount within its authorized "
                    "budget."
                ),
            )

        if constraints.accepts_installments:

            return (
                STATUS_COUNTER_OFFER,
                constraints.maximum_payment_amount,
                (
                    "Payer AP agent cannot authorize the full "
                    "amount immediately and proposes its "
                    "maximum authorized settlement amount "
                    "with the configured installment terms."
                ),
            )

        return (
            STATUS_REJECTED,
            amount,
            (
                "Payer AP agent cannot authorize the proposed "
                "amount and has no permitted fallback terms."
            ),
        )

    # ========================================================
    # Merchant counter strategy
    # ========================================================

    def merchant_counter_amount(
        self,
        original_amount: float,
        payer_amount: float,
        round_number: int,
    ) -> float:
        """
        Produce a bounded merchant counter-offer.

        Merchant discount schedule:

            Round 1 -> 0%
            Round 2 -> up to 5%
            Round 3+ -> policy maximum

        Never exceeds the merchant policy cap.
        """

        if round_number <= 1:

            target_discount = 0.0

        elif round_number == 2:

            target_discount = min(
                5.0,
                self.max_discount,
            )

        else:

            target_discount = (
                self.max_discount
            )

        merchant_amount = (
            original_amount
            * (
                1
                - target_discount / 100
            )
        )

        merchant_amount = max(
            merchant_amount,
            payer_amount,
        )

        return round(
            merchant_amount,
            2,
        )

    # ========================================================
    # Negotiation
    # ========================================================

    def negotiate(
        self,
        case: dict[str, Any],
        now: datetime | None = None,
    ) -> SettlementResult:

        if now is None:

            now = datetime.fromisoformat(
                case["timestamp"]
            )

        eligible, eligibility_reason = (
            self.is_eligible(
                case
            )
        )

        original_amount = float(
            case["amount"]
        )

        empty_policy = PolicyEvidence(
            allowed=False,
            checks=[],
            blocking_reasons=[],
        )

        # ----------------------------------------------------
        # Ineligible cases
        # ----------------------------------------------------

        if not eligible:

            return SettlementResult(
                case_id=case["case_id"],
                invoice_id=(
                    f"INV-{case['case_id']}"
                ),
                eligible=False,
                outcome=OUTCOME_BLOCKED,
                final_amount=0.0,
                discount_percent=0.0,
                rounds=0,
                reason=eligibility_reason,
                policy_evidence=empty_policy,
                transcript=[],
            )

        state = CaseState(
            case_id=case["case_id"]
        )

        payer_constraints = (
            self.build_payer_constraints(
                case
            )
        )

        transcript: list[
            NegotiationRound
        ] = []

        current_amount = (
            original_amount
        )

        final_policy = empty_policy

        # ----------------------------------------------------
        # Bounded negotiation
        # ----------------------------------------------------

        for round_number in range(
            1,
            self.max_rounds + 1,
        ):

            # ------------------------------------------------
            # Merchant proposal
            # ------------------------------------------------

            if round_number == 1:

                proposed_amount = (
                    original_amount
                )

                terms = (
                    "Full invoice settlement."
                )

            else:

                proposed_amount = (
                    self.merchant_counter_amount(
                        original_amount,
                        current_amount,
                        round_number,
                    )
                )

                if (
                    payer_constraints
                    .accepts_installments
                ):

                    terms = (
                        "Bounded settlement under merchant "
                        "discount policy; payer installment "
                        "terms may apply."
                    )

                else:

                    terms = (
                        "Bounded settlement under merchant "
                        "discount policy."
                    )

            expiry = (
                now.isoformat()
            )

            proposal = self.create_proposal(
                case=case,
                amount=proposed_amount,
                terms=terms,
                expiry=expiry,
            )

            # ------------------------------------------------
            # Merchant policy validation
            # ------------------------------------------------

            final_policy = (
                self.validate_proposal(
                    case=case,
                    round_number=round_number,
                    amount=proposed_amount,
                    state=state,
                    now=now,
                )
            )

            # ------------------------------------------------
            # Policy blocked
            # ------------------------------------------------

            if not final_policy.allowed:

                transcript.append(
                    NegotiationRound(
                        round_number=round_number,
                        merchant_amount=(
                            proposed_amount
                        ),
                        payer_amount=(
                            current_amount
                        ),
                        discount_percent=(
                            self.calculate_discount_percent(
                                original_amount,
                                proposed_amount,
                            )
                        ),
                        merchant_status=(
                            STATUS_BLOCKED
                        ),
                        payer_status=(
                            STATUS_REJECTED
                        ),
                        message=(
                            "Merchant policy blocked "
                            "the proposal: "
                            + "; ".join(
                                final_policy
                                .blocking_reasons
                            )
                        ),
                    )
                )

                return SettlementResult(
                    case_id=case["case_id"],
                    invoice_id=proposal.invoice_id,
                    eligible=True,
                    outcome=OUTCOME_BLOCKED,
                    final_amount=0.0,
                    discount_percent=0.0,
                    rounds=round_number,
                    reason=(
                        "A2A negotiation stopped because "
                        "merchant policy blocked the proposal."
                    ),
                    policy_evidence=final_policy,
                    transcript=transcript,
                )

            # ------------------------------------------------
            # Payer response
            # ------------------------------------------------

            try:

                (
                    payer_status,
                    payer_amount,
                    payer_message,
                ) = self.payer_agent_response(
                    proposal=proposal,
                    original_amount=original_amount,
                    constraints=payer_constraints,
                    round_number=round_number,
                    case=case,
                )

            except A2AClientError as exc:

                return SettlementResult(
                    case_id=case["case_id"],
                    invoice_id=proposal.invoice_id,
                    eligible=True,
                    outcome=OUTCOME_REJECTED,
                    final_amount=0.0,
                    discount_percent=0.0,
                    rounds=round_number,
                    reason=(
                        "A2A payer agent communication failed safely: "
                        f"{exc}"
                    ),
                    policy_evidence=final_policy,
                    transcript=transcript,
                    settlement_status="FAILED",
                    payment_status="NOT_STARTED",
                    recovery_confirmed=False,
                )

            discount_percent = (
                self.calculate_discount_percent(
                    original_amount,
                    proposed_amount,
                )
            )

            transcript.append(
                NegotiationRound(
                    round_number=round_number,
                    merchant_amount=(
                        proposed_amount
                    ),
                    payer_amount=(
                        payer_amount
                    ),
                    discount_percent=(
                        discount_percent
                    ),
                    merchant_status=(
                        STATUS_ACCEPTED
                    ),
                    payer_status=(
                        payer_status
                    ),
                    message=payer_message,
                )
            )

            # ------------------------------------------------
            # Accepted
            # ------------------------------------------------

            if (
                payer_status
                == STATUS_ACCEPTED
            ):

                final_amount = (
                    payer_amount
                )

                final_discount = (
                    self.calculate_discount_percent(
                        original_amount,
                        final_amount,
                    )
                )

                return SettlementResult(
                    case_id=case["case_id"],
                    invoice_id=proposal.invoice_id,
                    eligible=True,
                    outcome=OUTCOME_SETTLED,
                    final_amount=final_amount,
                    discount_percent=(
                        final_discount
                    ),
                    rounds=round_number,
                    reason=(
                        "Payer AP agent accepted a settlement "
                        "proposal within both merchant and "
                        "payer constraints."
                    ),
                    policy_evidence=(
                        final_policy
                    ),
                    transcript=transcript,
                    settlement_status="AGREED",
                    payment_status="PENDING",
                    recovery_confirmed=False,
                    agreement_id=self.build_idempotency_key(
                        case=case,
                        proposal=proposal,
                        round_number=round_number,
                    ),
                    a2a_agent_id=(
                        self.payer_agent_client.card.agent_id
                        if (
                            self.payer_agent_client is not None
                            and self.payer_agent_client.card is not None
                        )
                        else "synthetic-payer-agent"
                    ),
                    a2a_task_id=None,
                    a2a_context_id=None,
                )

            # ------------------------------------------------
            # Counter-offer
            # ------------------------------------------------

            if (
                payer_status
                == STATUS_COUNTER_OFFER
            ):

                current_amount = (
                    payer_amount
                )

                continue

            # ------------------------------------------------
            # Rejected
            # ------------------------------------------------

            return SettlementResult(
                case_id=case["case_id"],
                invoice_id=proposal.invoice_id,
                eligible=True,
                outcome=OUTCOME_REJECTED,
                final_amount=0.0,
                discount_percent=0.0,
                rounds=round_number,
                reason=(
                    "Payer AP agent rejected the settlement "
                    "proposal under its independent constraints."
                ),
                policy_evidence=final_policy,
                transcript=transcript,
            )

        # ----------------------------------------------------
        # Maximum rounds exhausted
        # ----------------------------------------------------

        return SettlementResult(
            case_id=case["case_id"],
            invoice_id=(
                f"INV-{case['case_id']}"
            ),
            eligible=True,
            outcome=OUTCOME_EXPIRED,
            final_amount=0.0,
            discount_percent=0.0,
            rounds=self.max_rounds,
            reason=(
                "Negotiation ended because the configured "
                "maximum number of rounds was reached."
            ),
            policy_evidence=final_policy,
            transcript=transcript,
        )


# ============================================================
# Serialization
# ============================================================

def settlement_to_dict(
    result: SettlementResult,
) -> dict[str, Any]:

    return asdict(
        result
    )


# ============================================================
# Formatting
# ============================================================

def print_transcript(
    result: SettlementResult,
) -> None:

    for round_data in result.transcript:

        print()
        print(
            f"  Round #{round_data.round_number}"
        )

        print(
            f"    Merchant amount: "
            f"₹{round_data.merchant_amount:,.2f}"
        )

        print(
            f"    Payer amount:    "
            f"₹{round_data.payer_amount:,.2f}"
        )

        print(
            f"    Discount:        "
            f"{round_data.discount_percent:.2f}%"
        )

        print(
            f"    Merchant:        "
            f"{round_data.merchant_status}"
        )

        print(
            f"    Payer:           "
            f"{round_data.payer_status}"
        )

        print(
            f"    Message:         "
            f"{round_data.message}"
        )


# ============================================================
# Main self-test
# ============================================================

def main() -> None:

    print("=" * 72)
    print(
        "REVIVE — MODULE 6"
    )
    print(
        "Agent-to-Agent Settlement Protocol"
    )
    print("=" * 72)

    cases = load_cases()

    print()
    print(
        f"Loaded cases: {len(cases)}"
    )

    engine = A2ASettlementEngine()

    # ========================================================
    # Configuration
    # ========================================================

    print()
    print(
        "A2A configuration:"
    )

    print(
        f"  Max rounds:       "
        f"{engine.max_rounds}"
    )

    print(
        f"  Max discount:     "
        f"{engine.max_discount:.2f}%"
    )

    print(
        "  ✓ Merchant policy controls negotiation."
    )

    print(
        "  ✓ Payer agent has independent constraints."
    )

    assert engine.max_rounds == 4

    assert engine.max_discount == 10.0

    # ========================================================
    # Eligibility
    # ========================================================

    eligible_cases = [
        case
        for case in cases
        if (
            case.get("surface")
            == "b2b_receivable"
            and case.get(
                "has_ap_agent",
                False,
            )
        )
    ]

    disputed_cases = [
        case
        for case in cases
        if (
            case.get("surface")
            == "b2b_receivable"
            and case.get(
                "root_cause_label"
            )
            == "invoice_dispute"
        )
    ]

    print()
    print(
        "Eligibility:"
    )

    print(
        f"  B2B cases with AP agent: "
        f"{len(eligible_cases)}"
    )

    print(
        f"  Disputed B2B cases:       "
        f"{len(disputed_cases)}"
    )

    assert len(eligible_cases) > 0

    assert len(disputed_cases) > 0

    # ========================================================
    # Run negotiations
    # ========================================================

    results: list[
        SettlementResult
    ] = []

    for case in eligible_cases:

        result = engine.negotiate(
            case
        )

        results.append(
            result
        )

    settled = [
        result
        for result in results
        if result.outcome
        == OUTCOME_SETTLED
    ]

    rejected = [
        result
        for result in results
        if result.outcome
        == OUTCOME_REJECTED
    ]

    blocked = [
        result
        for result in results
        if result.outcome
        == OUTCOME_BLOCKED
    ]

    expired = [
        result
        for result in results
        if result.outcome
        == OUTCOME_EXPIRED
    ]

    print()
    print(
        "Negotiation results:"
    )

    print(
        f"  Eligible negotiations: "
        f"{len(results)}"
    )

    print(
        f"  Settled:               "
        f"{len(settled)}"
    )

    print(
        f"  Rejected:              "
        f"{len(rejected)}"
    )

    print(
        f"  Blocked:               "
        f"{len(blocked)}"
    )

    print(
        f"  Expired:               "
        f"{len(expired)}"
    )

    # ========================================================
    # Sample negotiation
    # ========================================================

    if results:

        sample = results[0]

        print()
        print(
            "Sample A2A negotiation:"
        )

        print(
            f"  Case:       "
            f"{sample.case_id}"
        )

        print(
            f"  Invoice:    "
            f"{sample.invoice_id}"
        )

        print(
            f"  Outcome:    "
            f"{sample.outcome}"
        )

        print(
            f"  Final amount: "
            f"₹{sample.final_amount:,.2f}"
        )

        print(
            f"  Discount:   "
            f"{sample.discount_percent:.2f}%"
        )

        print(
            f"  Rounds:     "
            f"{sample.rounds}"
        )

        print(
            f"  Reason:     "
            f"{sample.reason}"
        )

        print_transcript(
            sample
        )

    # ========================================================
    # Disputed invoice safety
    # ========================================================

    print()
    print(
        "Disputed invoice safety check:"
    )

    for case in disputed_cases:

        result = engine.negotiate(
            case
        )

        assert (
            result.outcome
            == OUTCOME_BLOCKED
        )

        assert (
            result.eligible
            is False
        )

        assert (
            len(result.transcript)
            == 0
        )

        assert (
            result.policy_evidence
            .checks
            == []
        )

    print(
        "  ✓ Disputed invoices never enter A2A negotiation."
    )

    # ========================================================
    # Round bound
    # ========================================================

    print()
    print(
        "Round-bound verification:"
    )

    for result in results:

        assert (
            result.rounds
            <= engine.max_rounds
        )

        assert (
            len(result.transcript)
            <= engine.max_rounds
        )

    print(
        f"  ✓ No negotiation exceeded "
        f"{engine.max_rounds} rounds."
    )

    # ========================================================
    # Discount safety
    # ========================================================

    print()
    print(
        "Discount safety verification:"
    )

    for result in results:

        assert (
            result.discount_percent
            <= engine.max_discount
            + 0.000001
        )

        for round_data in (
            result.transcript
        ):

            assert (
                round_data.discount_percent
                <= engine.max_discount
                + 0.000001
            )

    print(
        f"  ✓ No proposal exceeded the "
        f"{engine.max_discount:.2f}% merchant discount cap."
    )

    # ========================================================
    # Authorization
    # ========================================================

    print()
    print(
        "Authorization test:"
    )

    if eligible_cases:

        proposal = engine.create_proposal(
            case=eligible_cases[0],
            amount=float(
                eligible_cases[0]["amount"]
            ),
            terms="Full settlement.",
            expiry=datetime.fromisoformat(
                eligible_cases[0]["timestamp"]
            ).isoformat(),
        )

        assert (
            proposal.authorization_proof
        )

        assert (
            proposal.invoice_id
        )

        assert (
            proposal.due_date
            is not None
        )

        assert (
            proposal.proposed_terms
        )

        assert (
            proposal.expiry
        )

        print(
            "  ✓ Settlement proposals contain "
            "invoice ID and authorization proof."
        )

        # ========================================================
        # ========================================================
    # Policy evidence
    # ========================================================

    print()
    print(
        "Policy evidence verification:"
    )

    total_policy_checks = 0
    policy_checked_results = 0
    blocked_results = 0

    for result in results:

        # ----------------------------------------------------
        # A2A results can be blocked before a proposal is
        # actually evaluated by the merchant policy engine.
        #
        # Therefore, do NOT require every result to contain
        # policy checks.
        # ----------------------------------------------------

        if result.outcome == OUTCOME_BLOCKED:

            blocked_results += 1

            assert result.reason

            continue

        # ----------------------------------------------------
        # Settled, rejected, or expired negotiations reached
        # proposal validation and therefore must contain
        # policy evidence.
        # ----------------------------------------------------

        assert (
            result.policy_evidence
            is not None
        )

        assert isinstance(
            result.policy_evidence,
            PolicyEvidence,
        )

        assert (
            len(
                result.policy_evidence.checks
            )
            > 0
        )

        policy_checked_results += 1

        for check in (
            result.policy_evidence.checks
        ):

            assert isinstance(
                check,
                dict,
            )

            assert (
                "name"
                in check
            )

            assert (
                "passed"
                in check
            )

            assert (
                "message"
                in check
            )

            total_policy_checks += 1

    # --------------------------------------------------------
    # At least one actual negotiation must have policy
    # evidence.
    # --------------------------------------------------------

    assert (
        policy_checked_results
        > 0
    )

    assert (
        total_policy_checks
        > 0
    )

    print(
        f"  ✓ {policy_checked_results} negotiation results "
        "contain explicit policy evidence."
    )

    print(
        f"  ✓ {total_policy_checks} individual policy checks "
        "were recorded."
    )

    print(
        f"  ✓ {blocked_results} blocked results contain "
        "explicit blocking reasons."
    )

    # --------------------------------------------------------
    # Disputed/ineligible cases must remain completely
    # outside the negotiation policy evaluation.
    # --------------------------------------------------------

    for case in disputed_cases:

        result = engine.negotiate(
            case
        )

        assert (
            result.eligible
            is False
        )

        assert (
            result.outcome
            == OUTCOME_BLOCKED
        )

        assert (
            result.policy_evidence.checks
            == []
        )

        assert (
            result.policy_evidence.blocking_reasons
            == []
        )

        assert (
            result.transcript
            == []
        )

    print(
        "  ✓ Ineligible cases correctly bypass "
        "settlement policy evaluation."
    )

    # ========================================================
    # Transcript
    # ========================================================

    print()
    print(
        "Transcript verification:"
    )

    total_rounds = sum(
        len(result.transcript)
        for result in results
    )

    print(
        f"  Total transcript rounds: "
        f"{total_rounds}"
    )

    for result in results:

        for round_data in (
            result.transcript
        ):

            assert (
                round_data.round_number
                >= 1
            )

            assert (
                round_data.message
            )

            assert (
                round_data.merchant_status
            )

            assert (
                round_data.payer_status
            )

            assert (
                round_data.merchant_amount
                > 0
            )

            assert (
                round_data.payer_amount
                > 0
            )

    print(
        "  ✓ Full negotiation transcript is auditable."
    )

    # ========================================================
    # JSON serialization
    # ========================================================

    print()
    print(
        "JSON serialization test:"
    )

    if results:

        exported = settlement_to_dict(
            results[0]
        )

        assert isinstance(
            exported,
            dict,
        )

        assert (
            exported["case_id"]
        )

        assert (
            exported["invoice_id"]
        )

        assert (
            "policy_evidence"
            in exported
        )

        assert (
            "transcript"
            in exported
        )

        assert isinstance(
            exported["transcript"],
            list,
        )

        print(
            "  ✓ Settlement results are JSON-friendly."
        )

    # ========================================================
    # Final integrity checks
    # ========================================================

    assert len(cases) == 105

    assert (
        len(results)
        == len(eligible_cases)
    )

    assert (
        len(settled)
        + len(rejected)
        + len(blocked)
        + len(expired)
        == len(results)
    )

    # ========================================================
    # Final
    # ========================================================

    print()
    print("=" * 72)
    print(
        "MODULE 6 SELF-TEST: PASSED"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()