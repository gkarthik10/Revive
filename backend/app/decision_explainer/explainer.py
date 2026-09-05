"""
REVIVE — Decision Explainer

Module responsible for explaining decisions that have already
been produced by the Revive recovery pipeline.

IMPORTANT DESIGN PRINCIPLE
--------------------------

The Decision Explainer NEVER decides whether a case should
be pursued or stopped.

The existing Revive pipeline remains the source of truth.

This module only:

    1. Reads the actual case result.
    2. Reads actual recovery ledger evidence.
    3. Reads actual policy evidence.
    4. Reads actual A2A settlement evidence.
    5. Builds a structured evidence package.
    6. Produces a human-readable explanation.
    7. Optionally uses an LLM to improve wording.

The LLM is therefore an explanation layer, not a decision layer.

LLM PROVIDER
------------

Groq is used as the optional LLM provider.

If GROQ_API_KEY is unavailable, the module automatically
falls back to the deterministic explanation engine.

The fallback is intentionally preserved so the feature
continues working without an external LLM.
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    from groq import Groq
except ImportError:
    Groq = None


# ============================================================
# Helpers
# ============================================================

def _number(
    value: Any,
    default: float = 0.0,
) -> float:
    """
    Safely convert a value to float.
    """

    try:
        if value is None:
            return default

        return float(value)

    except (TypeError, ValueError):
        return default


def _percent(
    value: Any,
) -> str:
    """
    Convert probability into percentage.

    Example:
        0.3825 -> 38.25%
    """

    return f"{_number(value) * 100:.2f}%"


def _currency(
    value: Any,
) -> str:
    """
    Format INR currency.
    """

    return f"₹{_number(value):,.2f}"


def _first_non_empty(
    source: dict[str, Any],
    *keys: str,
) -> Any:
    """
    Return the first non-empty value from a dictionary.
    """

    for key in keys:
        value = source.get(key)

        if value is not None and value != "":
            return value

    return None


def _normalise_reasons(
    value: Any,
) -> list[str]:
    """
    Convert reason data into a list of strings.
    """

    if value is None:
        return []

    if isinstance(value, list):
        return [
            str(item)
            for item in value
            if item is not None
            and str(item).strip()
        ]

    if isinstance(value, tuple):
        return [
            str(item)
            for item in value
            if item is not None
            and str(item).strip()
        ]

    if isinstance(value, str):
        if value.strip():
            return [value.strip()]

        return []

    return [str(value)]


# ============================================================
# Evidence Construction
# ============================================================

def build_case_evidence(
    case: dict[str, Any],
    ledger_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build the authoritative evidence package used by the
    explanation layer.

    No recovery decision is recalculated here.

    Pipeline case data is preferred.

    If ROI/policy values are not directly exposed by the case,
    the matching recovery ledger event is used as evidence.
    """

    ledger_events = ledger_events or []

    case_id = case.get("case_id")

    if not case_id:
        raise ValueError(
            "Case evidence requires a case_id."
        )

    # --------------------------------------------------------
    # Matching ledger history
    # --------------------------------------------------------

    case_ledger = [
        event
        for event in ledger_events
        if event.get("case_id") == case_id
    ]

    case_ledger.sort(
        key=lambda event: (
            event.get("attempt_number", 0),
            event.get("timestamp", ""),
        )
    )

    # --------------------------------------------------------
    # Last ledger event
    # --------------------------------------------------------

    last_event = (
        case_ledger[-1]
        if case_ledger
        else {}
    )

    # --------------------------------------------------------
    # Decision
    # --------------------------------------------------------

    decision = _first_non_empty(
        case,
        "roi_decision",
        "decision",
    )

    if decision is None:
        decision = last_event.get("decision")

    # --------------------------------------------------------
    # Outcome
    # --------------------------------------------------------

    outcome = _first_non_empty(
        case,
        "outcome",
    )

    if outcome is None:
        outcome = last_event.get("outcome")

    # --------------------------------------------------------
    # Attempt number
    # --------------------------------------------------------

    attempt_number = _first_non_empty(
        case,
        "roi_attempt_number",
        "attempt_number",
    )

    if attempt_number is None:
        attempt_number = last_event.get("attempt_number")

    # --------------------------------------------------------
    # Action
    # --------------------------------------------------------

    action = _first_non_empty(
        case,
        "action",
    )

    if action is None:
        action = last_event.get("action")

    # --------------------------------------------------------
    # Channel
    # --------------------------------------------------------

    channel = _first_non_empty(
        case,
        "channel",
    )

    if channel is None:
        channel = last_event.get("channel")

    # --------------------------------------------------------
    # ROI probability
    # --------------------------------------------------------

    probability = _first_non_empty(
        case,
        "roi_probability",
        "success_probability",
    )

    if probability is None:
        probability = last_event.get("success_probability")

    # --------------------------------------------------------
    # Expected recovery
    # --------------------------------------------------------

    expected_recovery = _first_non_empty(
        case,
        "expected_recovery",
    )

    if expected_recovery is None:
        expected_recovery = last_event.get("expected_recovery")

    # --------------------------------------------------------
    # Expected value
    # --------------------------------------------------------

    expected_value = _first_non_empty(
        case,
        "expected_value",
    )

    if expected_value is None:
        expected_value = last_event.get("expected_value")

    # --------------------------------------------------------
    # Action cost
    # --------------------------------------------------------

    action_cost = _first_non_empty(
        case,
        "action_cost",
    )

    if action_cost is None:
        action_cost = last_event.get("action_cost")

    # --------------------------------------------------------
    # Policy allowed
    # --------------------------------------------------------

    policy_allowed = case.get("policy_allowed")

    if policy_allowed is None:
        policy_allowed = last_event.get("policy_allowed")

    # --------------------------------------------------------
    # Policy blocking reasons
    # --------------------------------------------------------

    blocking_reasons = _normalise_reasons(
        case.get("policy_blocking_reasons")
    )

    if not blocking_reasons:
        blocking_reasons = _normalise_reasons(
            last_event.get("policy_blocking_reasons")
        )

    # --------------------------------------------------------
    # Recovery
    # --------------------------------------------------------

    recovered_amount = _first_non_empty(
        case,
        "recovered_amount",
    )

    if recovered_amount is None:
        if str(outcome).upper() == "RECOVERED":
            recovered_amount = last_event.get(
                "amount",
                0.0,
            )
        else:
            recovered_amount = 0.0

    # --------------------------------------------------------
    # A2A evidence
    # --------------------------------------------------------

    a2a_eligible = _first_non_empty(
        case,
        "a2a_eligible",
    )

    a2a_outcome = _first_non_empty(
        case,
        "a2a_outcome",
    )

    a2a_final_amount = _first_non_empty(
        case,
        "a2a_final_amount",
        "final_amount",
    )

    # --------------------------------------------------------
    # Final evidence package
    # --------------------------------------------------------

    return {
        "case": {
            "case_id": case_id,

            "customer_id": case.get(
                "customer_id"
            ),

            "surface": case.get(
                "surface"
            ),

            "amount": case.get(
                "amount"
            ),

            "root_cause": _first_non_empty(
                case,
                "root_cause",
                "root_cause_label",
            ),

            "action": action,

            "channel": channel,
        },

        "decision": {
            "roi_decision": decision,

            "outcome": outcome,

            "roi_attempt_number": attempt_number,
        },

        "roi": {
            "probability": probability,

            "probability_percent": _percent(
                probability
            ),

            "expected_recovery": expected_recovery,

            "expected_value": expected_value,

            "action_cost": action_cost,
        },

        "policy": {
            "allowed": policy_allowed,

            "blocking_reasons": blocking_reasons,
        },

        "recovery": {
            "recovered_amount": recovered_amount,
        },

        "a2a": {
            "eligible": a2a_eligible,

            "outcome": a2a_outcome,

            "final_amount": a2a_final_amount,
        },

        "ledger": {
            "event_count": len(case_ledger),

            "events": case_ledger,
        },
    }


# ============================================================
# Decision Explainer
# ============================================================

class DecisionExplainer:
    """
    Explain an already-computed Revive decision.

    Modes:

        fallback
            Deterministic explanation.

        llm
            Optional Groq explanation.

    The LLM never receives authority to make the decision.
    """

    def __init__(
        self,
        model: str | None = None,
    ) -> None:

        self.model = model or os.getenv(
            "REVIVE_EXPLAINER_MODEL",
            "openai/gpt-oss-120b",
        )

        self.api_key = os.getenv(
            "GROQ_API_KEY"
        )

        self.client = None

        if (
            self.api_key
            and Groq is not None
        ):
            self.client = Groq(
                api_key=self.api_key
            )

    # ========================================================
    # Public API
    # ========================================================

    def explain(
        self,
        evidence: dict[str, Any],
        question: str | None = None,
    ) -> dict[str, Any]:
        """
        Explain supplied evidence.
        """

        if self.client is not None:

            try:

                explanation = self._llm_explanation(
                    evidence=evidence,
                    question=question,
                )

                # ------------------------------------------------
                # Preserve authoritative fields from Revive.
                #
                # The LLM only explains the decision. It must
                # never be allowed to alter authoritative values.
                # ------------------------------------------------

                decision_evidence = evidence.get(
                    "decision",
                    {}
                )

                policy_evidence = evidence.get(
                    "policy",
                    {}
                )

                a2a_evidence = evidence.get(
                    "a2a",
                    {}
                )

                recovery_evidence = evidence.get(
                    "recovery",
                    {}
                )

                explanation["decision"] = (
                    decision_evidence.get(
                        "roi_decision"
                    )
                )

                explanation["outcome"] = (
                    decision_evidence.get(
                        "outcome"
                    )
                )

                explanation["attempt_number"] = (
                    decision_evidence.get(
                        "roi_attempt_number"
                    )
                )

                explanation["policy_allowed"] = (
                    policy_evidence.get(
                        "allowed"
                    )
                )

                explanation["a2a_outcome"] = (
                    a2a_evidence.get(
                        "outcome"
                    )
                )

                explanation["recovered_amount"] = (
                    recovery_evidence.get(
                        "recovered_amount"
                    )
                )

                explanation["question"] = question

                return {
                    "mode": "llm",

                    "explanation": explanation,

                    "evidence": evidence,
                }

            except Exception as exc:

                fallback = (
                    self._deterministic_explanation(
                        evidence=evidence,
                        question=question,
                    )
                )

                return {
                    "mode": "fallback",

                    "explanation": fallback,

                    "llm_error": str(exc),

                    "evidence": evidence,
                }

        return {
            "mode": "fallback",

            "explanation": (
                self._deterministic_explanation(
                    evidence=evidence,
                    question=question,
                )
            ),

            "evidence": evidence,
        }

    # ========================================================
    # Deterministic Explanation
    # ========================================================

    def _deterministic_explanation(
        self,
        evidence: dict[str, Any],
        question: str | None = None,
    ) -> dict[str, Any]:
        """
        Produce a grounded explanation.

        No new recovery decision is made.
        """

        case = evidence.get(
            "case",
            {}
        )

        decision = evidence.get(
            "decision",
            {}
        )

        roi = evidence.get(
            "roi",
            {}
        )

        policy = evidence.get(
            "policy",
            {}
        )

        recovery = evidence.get(
            "recovery",
            {}
        )

        a2a = evidence.get(
            "a2a",
            {}
        )

        ledger = evidence.get(
            "ledger",
            {}
        )

        a2a_outcome = a2a.get(
            "outcome"
        )

        case_id = case.get(
            "case_id",
            "this case",
        )

        amount = _number(
            case.get(
                "amount"
            )
        )

        action = case.get(
            "action",
            "the recovery action",
        )

        channel = case.get(
            "channel",
            "the configured channel",
        )

        root_cause = case.get(
            "root_cause",
            "an identified issue",
        )

        roi_decision = str(
            decision.get(
                "roi_decision",
                "UNKNOWN",
            )
        ).upper()

        outcome = str(
            decision.get(
                "outcome",
                "UNKNOWN",
            )
        ).upper()

        policy_allowed = policy.get(
            "allowed"
        )

        probability = _number(
            roi.get(
                "probability"
            )
        )

        expected_recovery = _number(
            roi.get(
                "expected_recovery"
            )
        )

        expected_value = _number(
            roi.get(
                "expected_value"
            )
        )

        action_cost = _number(
            roi.get(
                "action_cost"
            )
        )

        recovered_amount = _number(
            recovery.get(
                "recovered_amount"
            )
        )

        attempt_number = decision.get(
            "roi_attempt_number"
        )

        blocking_reasons = _normalise_reasons(
            policy.get(
                "blocking_reasons"
            )
        )

        reasons: list[str] = []

        # ====================================================
        # Policy evidence
        # ====================================================

        if policy_allowed is False:

            if blocking_reasons:

                for reason in blocking_reasons:

                    reasons.append(
                        f"Policy blocked the action: {reason}"
                    )

            else:

                reasons.append(
                    "Policy did not permit the recovery action."
                )

        elif policy_allowed is True:

            reasons.append(
                "Policy checks permitted the recovery action."
            )

        # ====================================================
        # A2A evidence
        # ====================================================

        if a2a_outcome == "SETTLED":

            reasons.append(
                "Agent-to-agent settlement completed successfully "
                "and is authoritative for the final recovery outcome."
            )

        elif a2a_outcome in {
            "REJECTED",
            "BLOCKED",
        }:

            reasons.append(
                f"Agent-to-agent settlement ended with "
                f"'{a2a_outcome}', so no A2A recovery was counted."
            )

        # ====================================================
        # Strategy evidence
        # ====================================================

        if action:

            reasons.append(
                f"The selected recovery action was "
                f"'{action}' through {channel}."
            )

        if root_cause:

            reasons.append(
                f"The diagnosed root cause was "
                f"'{root_cause}'."
            )

        # ====================================================
        # ROI evidence
        # ====================================================

        if (
            policy_allowed is not False
            and expected_value > 0
            and roi_decision == "PURSUE"
        ):

            reasons.append(
                f"The action had positive expected value "
                f"of {_currency(expected_value)}."
            )

        elif (
            policy_allowed is not False
            and expected_value <= 0
            and roi_decision in {
                "STOP",
                "STOPPED",
            }
        ):

            reasons.append(
                f"The expected value was "
                f"{_currency(expected_value)}, "
                "so Revive did not pursue the action."
            )

        # ====================================================
        # Probability
        # ====================================================

        if probability > 0:

            reasons.append(
                f"The estimated probability of successful "
                f"recovery was {_percent(probability)}."
            )

        # ====================================================
        # Outcome
        # ====================================================

        if outcome == "RECOVERED":

            reasons.append(
                f"The case recovered "
                f"{_currency(recovered_amount)}."
            )

        elif outcome in {
            "NOT_RECOVERED",
            "UNRECOVERED",
        }:

            reasons.append(
                "The recovery attempt did not produce "
                "a recovered amount."
            )

        # ====================================================
        # Ledger evidence
        # ====================================================

        event_count = ledger.get(
            "event_count",
            0,
        )

        if event_count:

            reasons.append(
                f"The recovery ledger contains "
                f"{event_count} recorded event"
                f"{'' if event_count == 1 else 's'} "
                f"for this case."
            )

        # ====================================================
        # Main summary
        # ====================================================

        if (
            policy_allowed is False
            and a2a_outcome == "SETTLED"
        ):

            summary = (
                f"REVIVE's orchestrated {action} for "
                f"{case_id} was blocked by policy, but the "
                f"case was still recovered — "
                f"{_currency(recovered_amount)} — through "
                f"agent-to-agent settlement, a separate "
                f"channel that is not subject to the same "
                f"human-contact-hours restriction."
            )

        elif policy_allowed is False:

            summary = (
                f"REVIVE stopped {case_id} because "
                f"the {action} recovery action was "
                f"not permitted by policy."
            )

        elif a2a_outcome == "SETTLED":

            summary = (
                f"REVIVE recovered {case_id} through "
                f"agent-to-agent settlement, with "
                f"{_currency(recovered_amount)} recovered."
            )

        elif a2a_outcome in {
            "REJECTED",
            "BLOCKED",
        }:

            summary = (
                f"REVIVE did not recover {case_id} through "
                f"agent-to-agent settlement because the "
                f"settlement outcome was {a2a_outcome}."
            )

        elif roi_decision in {
            "STOP",
            "STOPPED",
        }:

            summary = (
                f"REVIVE stopped {case_id} because "
                f"the recovery action was not "
                f"economically justified."
            )

        elif roi_decision == "PURSUE":

            summary = (
                f"REVIVE pursued {case_id} because "
                f"the recovery action was permitted "
                f"and economically justified."
            )

        else:

            summary = (
                f"REVIVE recorded a "
                f"{roi_decision} decision for "
                f"{case_id}."
            )

        # ====================================================
        # Financial explanation
        # ====================================================

        if (
            policy_allowed is False
            and a2a_outcome == "SETTLED"
        ):

            financial_explanation = (
                f"{_currency(amount)} was at risk. The "
                f"orchestrated action's own cost/probability "
                f"model never ran, since policy blocked it — "
                f"but the case was independently recovered in "
                f"full via agent-to-agent settlement: "
                f"{_currency(recovered_amount)} recovered."
            )

        elif policy_allowed is False:

            financial_explanation = (
                f"{_currency(amount)} was at risk, "
                f"but the policy gate blocked the action. "
                f"Therefore no recovery action cost was "
                f"recorded for this blocked attempt, and "
                f"expected recovery was "
                f"{_currency(expected_recovery)}."
            )

        elif a2a_outcome == "SETTLED":

            financial_explanation = (
                f"The case had {_currency(amount)} at risk. "
                f"Agent-to-agent settlement recovered "
                f"{_currency(recovered_amount)} as the "
                f"authoritative final recovery amount."
            )

        elif a2a_outcome in {
            "REJECTED",
            "BLOCKED",
        }:

            financial_explanation = (
                f"The case had {_currency(amount)} at risk, "
                f"but the A2A settlement outcome was "
                f"{a2a_outcome}. Therefore A2A contributed "
                f"₹0.00 to recovered revenue."
            )

        else:

            financial_explanation = (
                f"The case had {_currency(amount)} at risk. "
                f"The estimated recovery was "
                f"{_currency(expected_recovery)} against "
                f"an action cost of {_currency(action_cost)}, "
                f"producing expected value of "
                f"{_currency(expected_value)}."
            )

        # ====================================================
        # Policy explanation
        # ====================================================

        if policy_allowed is False:

            if blocking_reasons:

                policy_explanation = (
                    "The policy engine blocked the action "
                    "for the following reason(s): "
                    + " ".join(
                        blocking_reasons
                    )
                )

            else:

                policy_explanation = (
                    "The policy engine did not permit "
                    "the recovery action."
                )

        elif policy_allowed is True:

            policy_explanation = (
                "The policy engine permitted the "
                "recovery action."
            )

        else:

            policy_explanation = (
                "Policy permission is not available "
                "in the supplied evidence."
            )

        # ====================================================
        # Audit note
        # ====================================================

        audit_note = (
            "This explanation is grounded in the "
            "Revive pipeline result, A2A settlement evidence, "
            "and recovery ledger. The explanation layer does "
            "not recalculate or override the recovery decision."
        )

        return {
            "summary": summary,

            "question": question,

            "decision": roi_decision,

            "outcome": outcome,

            "policy_allowed": policy_allowed,

            "a2a_outcome": a2a_outcome,

            "recovered_amount": recovered_amount,

            "reasons": reasons,

            "financial_explanation": (
                financial_explanation
            ),

            "policy_explanation": (
                policy_explanation
            ),

            "audit_note": audit_note,

            "attempt_number": attempt_number,
        }

    # ========================================================
    # Groq LLM Explanation
    # ========================================================

    def _llm_explanation(
        self,
        evidence: dict[str, Any],
        question: str | None,
    ) -> dict[str, Any]:
        """
        Ask Groq to explain supplied evidence.

        Groq is strictly an explanation layer.
        It does not make or modify the Revive decision.
        """

        evidence_json = json.dumps(
            evidence,
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        user_question = (
            question.strip()
            if question
            else "Explain why REVIVE made this decision."
        )

        system_prompt = """
You are the explanation layer of REVIVE,
an AI revenue recovery system.

Your ONLY task is to explain an existing decision.

You are NOT the recovery decision engine.

STRICT RULES:

1. Do not make a new recovery decision.
2. Do not change the supplied decision.
3. Do not invent numbers.
4. Do not invent policy rules.
5. Do not invent customer information.
6. Do not invent events.
7. Use ONLY the supplied evidence.
8. Clearly distinguish policy restrictions from ROI economics.
9. Clearly distinguish A2A settlement from human-channel policy.
10. If A2A outcome is SETTLED, treat that as authoritative
    evidence that the case recovered through A2A.
11. If A2A outcome is REJECTED or BLOCKED, do not describe
    the A2A case as recovered.
12. If evidence is unavailable, explicitly say it is unavailable.
13. Treat the recovery ledger as audit evidence.
14. Keep all financial reasoning consistent with supplied values.
15. Never calculate a different decision from the supplied decision.
16. Never recommend pursuing or stopping a case.
17. Keep the explanation concise and professional.
18. The output is for a financial recovery operations dashboard.

Return ONLY valid JSON.

Use exactly these fields:

{
  "summary": "short natural-language explanation",
  "decision": "existing decision from evidence",
  "outcome": "existing outcome from evidence",
  "reasons": [
    "grounded reason 1",
    "grounded reason 2"
  ],
  "financial_explanation": "grounded financial explanation",
  "policy_explanation": "grounded policy explanation",
  "audit_note": "statement that the explanation is grounded in Revive audit evidence"
}
"""

        user_prompt = f"""
USER QUESTION:

{user_question}

AUTHORITATIVE REVIVE EVIDENCE:

{evidence_json}
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=0,
            max_tokens=900,
            response_format={
                "type": "json_object"
            },
        )

        text = (
            response.choices[0]
            .message
            .content
            or ""
        ).strip()

        try:

            parsed = json.loads(
                text
            )

            if isinstance(
                parsed,
                dict,
            ):

                return parsed

        except json.JSONDecodeError:

            pass

        # ----------------------------------------------------
        # If Groq returned non-JSON text, preserve the text
        # instead of losing the explanation.
        # ----------------------------------------------------

        decision = evidence.get(
            "decision",
            {}
        )

        return {
            "summary": text,

            "decision": decision.get(
                "roi_decision"
            ),

            "outcome": decision.get(
                "outcome"
            ),

            "reasons": [],

            "financial_explanation": (
                "See the structured financial "
                "evidence returned with this explanation."
            ),

            "policy_explanation": (
                "See the structured policy "
                "evidence returned with this explanation."
            ),

            "audit_note": (
                "Explanation generated from supplied "
                "Revive audit evidence."
            ),
        }


# ============================================================
# Self-Test
# ============================================================

def main() -> None:

    print("=" * 72)
    print("REVIVE — DECISION EXPLAINER")
    print("=" * 72)

    case = {
        "case_id": "RV-TEST-001",
        "customer_id": "CUST-TEST",
        "surface": "subscription_failure",
        "amount": 10000,
        "root_cause": "card_expired",
        "action": "whatsapp",
        "channel": "whatsapp",
    }

    ledger = [
        {
            "case_id": "RV-TEST-001",
            "attempt_number": 1,
            "timestamp": "2026-08-30T10:00:00",
            "action": "whatsapp",
            "channel": "whatsapp",
            "amount": 10000,
            "success_probability": 0.35,
            "expected_recovery": 3500,
            "action_cost": 2,
            "expected_value": 3498,
            "decision": "PURSUE",
            "outcome": "NOT_RECOVERED",
            "reason": "Positive expected value.",
            "policy_allowed": True,
            "policy_blocking_reasons": [],
        },
        {
            "case_id": "RV-TEST-001",
            "attempt_number": 2,
            "timestamp": "2026-08-30T11:00:00",
            "action": "whatsapp",
            "channel": "whatsapp",
            "amount": 10000,
            "success_probability": 0.245,
            "expected_recovery": 2450,
            "action_cost": 2,
            "expected_value": 2448,
            "decision": "PURSUE",
            "outcome": "RECOVERED",
            "reason": "Positive expected value.",
            "policy_allowed": True,
            "policy_blocking_reasons": [],
        },
    ]

    evidence = build_case_evidence(
        case=case,
        ledger_events=ledger,
    )

    explainer = DecisionExplainer()

    result = explainer.explain(
        evidence=evidence,
        question="Why was this case pursued?",
    )

    # --------------------------------------------------------
    # Explanation must always exist.
    # --------------------------------------------------------

    assert result["explanation"]

    assert result["mode"] in {
        "fallback",
        "llm",
    }

    assert (
        result["evidence"]["ledger"]["event_count"]
        == 2
    )

    assert (
        result["evidence"]["roi"]["probability"]
        == 0.245
    )

    print()
    print("✓ Evidence construction passed.")

    print("✓ Multi-attempt ledger history passed.")

    print("✓ Ledger fallback values passed.")

    print(
        f"✓ Explanation mode: "
        f"{result['mode'].upper()}"
    )

    print("✓ Explanation generation passed.")

    # --------------------------------------------------------
    # A2A explanation verification
    # --------------------------------------------------------

    a2a_case = {
        "case_id": "RV-A2A-TEST",
        "customer_id": "CUST-A2A",
        "surface": "b2b_receivable",
        "amount": 100000,
        "root_cause": "b2b_cashflow_delay",
        "action": "human_escalation",
        "channel": "human_finance",
        "roi_decision": "STOP",
        "outcome": "RECOVERED",
        "recovered_amount": 100000,
        "policy_allowed": False,
        "policy_blocking_reasons": [
            "Human contact outside allowed hours."
        ],
        "a2a_eligible": True,
        "a2a_outcome": "SETTLED",
        "a2a_final_amount": 100000,
    }

    a2a_evidence = build_case_evidence(
        case=a2a_case,
        ledger_events=[],
    )

    # Use deterministic mode explicitly for this verification.
    #
    # This ensures the A2A assertions test the actual
    # deterministic Revive explanation logic rather than
    # depending on external LLM availability.

    a2a_explainer = DecisionExplainer()

    a2a_explanation = (
        a2a_explainer._deterministic_explanation(
            evidence=a2a_evidence,
            question="Why was this case recovered?",
        )
    )

    assert (
        a2a_evidence["a2a"]["outcome"]
        == "SETTLED"
    )

    assert (
        a2a_explanation["a2a_outcome"]
        == "SETTLED"
    )

    assert (
        a2a_explanation["outcome"]
        == "RECOVERED"
    )

    assert (
        a2a_explanation["recovered_amount"]
        == 100000
    )

    assert (
        "agent-to-agent"
        in a2a_explanation["summary"].lower()
    )

    print("✓ A2A evidence capture passed.")

    print("✓ A2A settlement explanation passed.")

    print()
    print("Decision:")

    print(
        f"  {result['explanation']['decision']}"
    )

    print()
    print("Summary:")

    print(
        f"  {result['explanation']['summary']}"
    )

    print()
    print("=" * 72)
    print("DECISION EXPLAINER SELF-TEST: PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()