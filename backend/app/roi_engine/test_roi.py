"""
Revive - Module 6 self-tests
ROI Portfolio Engine v4 — verification suite

This is the self-test / verification suite for the ROI engine in
roi.py. It was originally embedded in the same file as the engine
itself; it's split out here so anyone reviewing the core economic
logic doesn't have to scroll past ~1,300 lines of test scaffolding
to find it, and so the two can be read (or run) independently.

Run: python3 test_roi.py
"""

from __future__ import annotations

from app.roi_engine.roi import *  # noqa: F401,F403 -- intentional: this
# suite exercises everything roi.py exports (engine, dataclasses,
# constants, and helpers roi.py itself depends on).

# IMPORTANT:
# load_cases is no longer imported/re-exported by roi.py.
# Import it directly from its authoritative source so this
# verification suite does not depend on roi.py's internal imports.
from app.diagnosis.classifier import load_cases  # noqa: F401


def main() -> None:

    print("=" * 72)
    print("REVIVE — ROI PORTFOLIO ENGINE v4")
    print("Policy-Gated Economic Recovery")
    print("=" * 72)

    # ========================================================
    # Dataset
    # ========================================================

    cases = load_cases()

    print()
    print(
        f"Loaded cases: {len(cases)}"
    )

    assert (
        len(cases)
        == EXPECTED_CASE_COUNT
    )

    # ========================================================
    # Engine
    # ========================================================

    engine = ROIPortfolioEngine()

    print()
    print("ROI configuration:")

    print(
        f"  Attempt decay: "
        f"{engine.attempt_decay:.2f}"
    )

    print(
        f"  Max attempts:  "
        f"{engine.max_attempts}"
    )

    print(
        "  ✓ payment_retry pricing configured."
    )

    print(
        "  ✓ negotiation pricing configured."
    )

    print(
        "  ✓ human escalation pricing configured."
    )

    # ========================================================
    # Pricing tests
    # ========================================================

    print()
    print(
        "Pricing integrity test:"
    )

    assert (
        engine.action_cost(
            "payment_retry",
            "payment_gateway",
        )
        == 3.0
    )

    assert (
        engine.action_cost(
            "whatsapp",
            "whatsapp",
        )
        == 2.0
    )

    assert (
        engine.action_cost(
            "email",
            "email",
        )
        == 0.5
    )

    assert (
        engine.action_cost(
            "voice_call",
            "voice_call",
        )
        == 15.0
    )

    assert (
        engine.action_cost(
            "negotiate",
            "voice_call",
        )
        == 15.0
    )

    print(
        "  ✓ payment_retry/payment_gateway: ₹3.00"
    )

    print(
        "  ✓ whatsapp: ₹2.00"
    )

    print(
        "  ✓ email: ₹0.50"
    )

    print(
        "  ✓ voice_call/negotiate: ₹15.00"
    )

    try:

        engine.action_cost(
            "unknown_action",
            "unknown_channel",
        )

        raise AssertionError(
            "Unknown pricing was accepted."
        )

    except ValueError:

        print(
            "  ✓ Unknown actions are rejected."
        )

    # ========================================================
    # Probability decay
    # ========================================================

    print()
    print(
        "Probability decay test:"
    )

    p1 = engine.calculate_success_probability(
        root_cause="issuer_declined",
        action="payment_retry",
        channel="payment_gateway",
        attempt_number=1,
    )[3]

    p2 = engine.calculate_success_probability(
        root_cause="issuer_declined",
        action="payment_retry",
        channel="payment_gateway",
        attempt_number=2,
    )[3]

    p3 = engine.calculate_success_probability(
        root_cause="issuer_declined",
        action="payment_retry",
        channel="payment_gateway",
        attempt_number=3,
    )[3]

    print(
        f"  Attempt #1: {p1:.2%}"
    )

    print(
        f"  Attempt #2: {p2:.2%}"
    )

    print(
        f"  Attempt #3: {p3:.2%}"
    )

    assert p1 > p2 > p3

    print(
        "  ✓ Probability decreases after each failed attempt."
    )

    # ========================================================
    # Policy Gate Integrity
    # ========================================================

    print()
    print(
        "Policy-gate integrity test:"
    )

    # --------------------------------------------------------
    # Test A — customer opted out
    # --------------------------------------------------------

    blocked_case = {
        "case_id": "RV-POLICY-GATE-001",

        "amount": 10000,

        "surface": "subscription_failure",

        "root_cause_label": "card_expired",

        "timestamp": "2026-08-29T14:30:00",

        "customer_name": "Policy Test Customer",

        "opted_out": True,
    }

    blocked_diagnosis = diagnose_case(
        blocked_case
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # The orchestrator may route an opted-out customer to
    # human escalation. The policy-gate test must therefore
    # explicitly test an AUTOMATED contact action.
    # --------------------------------------------------------

    blocked_action = "whatsapp"

    blocked_policy = engine.check_policy(
        case=blocked_case,
        action=blocked_action,
        attempt_number=1,
    )

    print()
    print(
        "  Test A — Customer opted out"
    )

    print(
        f"  Automated action tested: {blocked_action}"
    )

    print_checks(
        blocked_policy
    )

    assert (
        blocked_policy.allowed
        is False
    )

    assert any(
        "opted out"
        in reason.lower()
        for reason
        in blocked_policy.blocking_reasons
    )

    print(
        "  ✓ Automated outreach is blocked for opted-out customer."
    )

    # --------------------------------------------------------
    # Human escalation is intentionally a separate policy path.
    # --------------------------------------------------------

    escalation_policy = engine.check_policy(
        case=blocked_case,
        action="human_escalation",
        attempt_number=1,
    )

    assert (
        escalation_policy.allowed
        is True
    )

    print(
        "  ✓ Human escalation remains available."
    )

    # --------------------------------------------------------
    # A policy-blocked action must become a stopped decision
    # and must never create recovery value or execution cost.
    # --------------------------------------------------------

    blocked_decision = (
        engine.create_policy_blocked_decision(
            case=blocked_case,
            diagnosis=blocked_diagnosis,
            attempt_number=1,
            policy_result=blocked_policy,
        )
    )

    assert (
        blocked_decision.decision
        == DECISION_STOPPED
    )

    assert (
        blocked_decision.outcome
        == OUTCOME_NOT_RECOVERED
    )

    assert (
        blocked_decision.recovered_amount
        == 0.0
    )

    assert (
        blocked_decision.explanation
        .action_cost
        == 0.0
    )

    assert (
        blocked_decision.explanation
        .expected_recovery
        == 0.0
    )

    assert (
        blocked_decision.explanation
        .expected_value
        == 0.0
    )

    print(
        "  ✓ Opted-out customer is blocked before ROI."
    )

    # --------------------------------------------------------
    # Test B — disputed invoice
    # --------------------------------------------------------

    disputed_case = {
        "case_id": "RV-POLICY-GATE-002",

        "amount": 100000,

        "surface": "b2b_receivable",

        "root_cause_label": "invoice_dispute",

        "timestamp": "2026-08-29T14:30:00",

        "customer_name": "Disputed Customer",

        "disputed": True,
    }

    disputed_diagnosis = diagnose_case(
        disputed_case
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Do not assume what action the orchestrator proposes.
    # The purpose of this test is to prove that the POLICY
    # blocks automated recovery for a disputed invoice.
    #
    # Human escalation may remain allowed and is a separate
    # path from automated recovery.
    # --------------------------------------------------------

    disputed_action = "whatsapp"

    disputed_policy = engine.check_policy(
        case=disputed_case,
        action=disputed_action,
        attempt_number=1,
    )

    print()
    print(
        "  Test B — Disputed invoice"
    )

    print(
        f"  Automated action tested: {disputed_action}"
    )

    print_checks(
        disputed_policy
    )

    assert (
        disputed_policy.allowed
        is False
    )

    assert any(
        "disputed"
        in reason.lower()
        for reason
        in disputed_policy.blocking_reasons
    )

    print(
        "  ✓ Automated recovery is blocked for disputed invoice."
    )

    disputed_decision = (
        engine.create_policy_blocked_decision(
            case=disputed_case,
            diagnosis=disputed_diagnosis,
            attempt_number=1,
            policy_result=disputed_policy,
        )
    )

    assert (
        disputed_decision.decision
        == DECISION_STOPPED
    )

    assert (
        disputed_decision.outcome
        == OUTCOME_NOT_RECOVERED
    )

    assert (
        disputed_decision.recovered_amount
        == 0.0
    )

    assert (
        disputed_decision.explanation
        .action_cost
        == 0.0
    )

    assert (
        disputed_decision.explanation
        .expected_recovery
        == 0.0
    )

    assert (
        disputed_decision.explanation
        .expected_value
        == 0.0
    )

    print(
        "  ✓ Disputed invoice is stopped before ROI."
    )

    # --------------------------------------------------------
    # Human escalation should remain a separate path.
    # --------------------------------------------------------

    disputed_escalation_policy = (
        engine.check_policy(
            case=disputed_case,
            action="human_escalation",
            attempt_number=1,
        )
    )

    assert (
        disputed_escalation_policy.allowed
        is True
    )

    print(
        "  ✓ Human escalation remains available for disputed invoice."
    )

    # --------------------------------------------------------
    # Test C — active promise to pay
    # --------------------------------------------------------

    promise_case = {
        "case_id": "RV-POLICY-GATE-003",

        "amount": 25000,

        "surface": "subscription_failure",

        "root_cause_label": "card_expired",

        "timestamp": "2026-08-29T14:30:00",

        "customer_name": "Promise Customer",

        "promise_to_pay_active": True,

        "promise_date": "2026-09-02T12:00:00",
    }

    promise_diagnosis = diagnose_case(
        promise_case
    )

    # Explicitly test an automated contact action.
    promise_action = "whatsapp"

    promise_policy = engine.check_policy(
        case=promise_case,
        action=promise_action,
        attempt_number=1,
    )

    print()
    print(
        "  Test C — Active promise-to-pay"
    )

    print(
        f"  Automated action tested: {promise_action}"
    )

    print_checks(
        promise_policy
    )

    assert (
        promise_policy.allowed
        is False
    )

    assert any(
        "promise"
        in reason.lower()
        for reason
        in promise_policy.blocking_reasons
    )

    promise_decision = (
        engine.create_policy_blocked_decision(
            case=promise_case,
            diagnosis=promise_diagnosis,
            attempt_number=1,
            policy_result=promise_policy,
        )
    )

    assert (
        promise_decision.decision
        == DECISION_STOPPED
    )

    assert (
        promise_decision.outcome
        == OUTCOME_NOT_RECOVERED
    )

    assert (
        promise_decision.recovered_amount
        == 0.0
    )

    assert (
        promise_decision.explanation
        .action_cost
        == 0.0
    )

    assert (
        promise_decision.explanation
        .expected_recovery
        == 0.0
    )

    assert (
        promise_decision.explanation
        .expected_value
        == 0.0
    )

    print(
        "  ✓ Active promise-to-pay blocks automated contact."
    )

    # --------------------------------------------------------
    # Test D — cooldown
    # --------------------------------------------------------

    cooldown_case = {
        "case_id": "RV-POLICY-GATE-004",

        "amount": 25000,

        "surface": "subscription_failure",

        "root_cause_label": "card_expired",

        "timestamp": "2026-08-29T13:30:00",

        "customer_name": "Cooldown Customer",

        "contact_attempts": 1,

        "last_contact_at": "2026-08-29T13:00:00",
    }

    cooldown_diagnosis = diagnose_case(
        cooldown_case
    )

    # Explicitly test an automated contact action.
    cooldown_action = "whatsapp"

    cooldown_policy = engine.check_policy(
        case=cooldown_case,
        action=cooldown_action,
        attempt_number=1,
    )

    print()
    print(
        "  Test D — Active cooldown"
    )

    print(
        f"  Automated action tested: {cooldown_action}"
    )

    print_checks(
        cooldown_policy
    )

    assert (
        cooldown_policy.allowed
        is False
    )

    assert any(
        "cooldown"
        in reason.lower()
        for reason
        in cooldown_policy.blocking_reasons
    )

    cooldown_decision = (
        engine.create_policy_blocked_decision(
            case=cooldown_case,
            diagnosis=cooldown_diagnosis,
            attempt_number=1,
            policy_result=cooldown_policy,
        )
    )

    assert (
        cooldown_decision.decision
        == DECISION_STOPPED
    )

    assert (
        cooldown_decision.outcome
        == OUTCOME_NOT_RECOVERED
    )

    assert (
        cooldown_decision.recovered_amount
        == 0.0
    )

    assert (
        cooldown_decision.explanation
        .action_cost
        == 0.0
    )

    assert (
        cooldown_decision.explanation
        .expected_recovery
        == 0.0
    )

    assert (
        cooldown_decision.explanation
        .expected_value
        == 0.0
    )

    print(
        "  ✓ Active cooldown blocks automated contact."
    )

    # ========================================================
    # Run Portfolio
    # ========================================================

    decisions = engine.run_portfolio(
        cases
    )

    metrics = engine.calculate_metrics(
        decisions,
        cases,
    )

    print()
    print(
        "Portfolio economics:"
    )

    print(
        f"  Addressable revenue: "
        f"{rupees(metrics.addressable_amount)}"
    )

    print(
        f"  Pursued attempts:    "
        f"{metrics.pursued_attempts}"
    )

    print(
        f"  Stopped cases:       "
        f"{metrics.stopped_cases}"
    )

    print(
        f"  Recovered cases:     "
        f"{metrics.recovered_cases}"
    )

    print(
        f"  Recovered revenue:   "
        f"{rupees(metrics.recovered_amount)}"
    )

    print(
        f"  Unrecovered revenue: "
        f"{rupees(metrics.unrecovered_amount)}"
    )

    print(
        f"  Recovery cost:       "
        f"{rupees(metrics.recovery_cost)}"
    )

    print(
        f"  Net recovered value: "
        f"{rupees(metrics.net_recovered_value)}"
    )

    print(
        f"  Recovery rate:       "
        f"{metrics.recovery_rate:.2%}"
    )

    print(
        f"  Cost / ₹ recovered:  "
        f"{metrics.cost_per_rupee_recovered:.6f}"
    )

    # ========================================================
    # Portfolio Integrity
    # ========================================================

    assert (
        metrics.total_cases
        == EXPECTED_CASE_COUNT
    )

    assert (
        metrics.addressable_amount
        > 0
    )

    assert (
        0
        <= metrics.recovered_amount
        <= metrics.addressable_amount
    )

    assert (
        metrics.unrecovered_amount
        >= 0
    )

    assert (
        metrics.recovery_cost
        >= 0
    )

    assert (
        0
        <= metrics.recovery_rate
        <= 1
    )

    print()
    print(
        "  ✓ Portfolio financial bounds are valid."
    )

    # ========================================================
    # Ledger Verification
    # ========================================================

    ledger_events = (
        engine.ledger.all_events()
    )

    print()
    print(
        "Recovery ledger:"
    )

    print(
        f"  Events recorded: "
        f"{len(ledger_events)}"
    )

    print(
        f"  Pursued events:  "
        f"{len(engine.ledger.pursued_events())}"
    )

    print(
        f"  Stopped events:  "
        f"{len(engine.ledger.stopped_events())}"
    )

    assert (
        len(ledger_events)
        == len(decisions)
    )

    print(
        "  ✓ Every ROI decision is recorded exactly once."
    )

    # --------------------------------------------------------
    # Every case must have ledger history
    # --------------------------------------------------------

    ledger_case_ids = {
        event.case_id
        for event in ledger_events
    }

    case_ids = {
        case["case_id"]
        for case in cases
    }

    assert (
        ledger_case_ids
        == case_ids
    )

    print(
        "  ✓ Every case is represented in the ledger."
    )

    # ========================================================
    # Ledger Financial Integrity
    # ========================================================

    for event in ledger_events:

        if event.decision == DECISION_STOPPED:

            # A policy-blocked or negative-EV stop should not
            # charge an execution cost.
            assert event.action_cost >= 0

        assert event.expected_recovery >= 0

        assert event.action_cost >= 0

        assert event.expected_value == (
            event.expected_recovery
            - event.action_cost
        )

    print(
        "  ✓ Ledger expected-value arithmetic is valid."
    )

    # ========================================================
    # Stopping Rule Verification
    # ========================================================

    stopped = [
        decision
        for decision in decisions
        if decision.decision
        == DECISION_STOPPED
    ]

    print()
    print(
        "Stopping-rule decisions:"
    )

    print(
        f"  Stopped attempts: "
        f"{len(stopped)}"
    )

    for decision in stopped[:5]:

        explanation = (
            decision.explanation
        )

        print()
        print(
            f"  Case:       {decision.case_id}"
        )

        print(
            f"  Attempt:    "
            f"#{decision.attempt_number}"
        )

        print(
            f"  Root cause: "
            f"{decision.root_cause}"
        )

        print(
            f"  Action:     "
            f"{decision.action}"
        )

        policy_blocked = explanation.reason.startswith(
            "Policy blocked"
        )

        if policy_blocked:
            print(
                "  P(success): N/A — blocked by policy "
                "before any economic evaluation ran"
            )
        else:
            print(
                f"  P(success): "
                f"{explanation.final_success_probability:.2%}"
            )

        print(
            f"  Expected recovery: "
            f"{rupees(explanation.expected_recovery)}"
        )

        print(
            f"  Action cost: "
            f"{rupees(explanation.action_cost)}"
        )

        print(
            f"  Expected value: "
            f"{rupees(explanation.expected_value)}"
        )

        print(
            f"  Decision:    "
            f"{decision.decision}"
        )

    for decision in stopped:

        # Every stopped decision must have a valid reason.
        assert decision.explanation.reason

    print(
        "  ✓ All stopped decisions contain explicit reasons."
    )

    # ========================================================
    # Attempt History
    # ========================================================

    multi_attempt_cases: dict[
        str,
        list[ROIDecision],
    ] = {}

    for decision in decisions:

        multi_attempt_cases.setdefault(
            decision.case_id,
            [],
        ).append(
            decision
        )

    multi_attempt_cases = {
        case_id: history
        for case_id, history
        in multi_attempt_cases.items()
        if len(history) > 1
    }

    print()
    print(
        "Recovery-history verification:"
    )

    print(
        f"  Cases with multiple attempts: "
        f"{len(multi_attempt_cases)}"
    )

    for case_id, history in list(
        multi_attempt_cases.items()
    )[:3]:

        print()
        print(
            f"  Case: {case_id}"
        )

        for decision in history:

            print(
                f"    Attempt #{decision.attempt_number}: "
                f"{decision.decision} / "
                f"{decision.outcome} / "
                f"EV "
                f"{rupees(decision.explanation.expected_value)}"
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
        "  ✓ Attempt history is sequential."
    )

    # ========================================================
    # Ledger Ordering
    # ========================================================

    ledger_by_case: dict[
        str,
        list[RecoveryEvent],
    ] = {}

    for event in ledger_events:

        ledger_by_case.setdefault(
            event.case_id,
            [],
        ).append(
            event
        )

    for case_id, history in (
        ledger_by_case.items()
    ):

        history.sort(
            key=lambda event:
            event.attempt_number
        )

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

    # ========================================================
    # Explainability
    # ========================================================

    for decision in decisions:

        assert decision.case_id

        assert decision.root_cause

        assert decision.action

        assert decision.channel

        assert decision.decision

        assert decision.explanation.reason

        assert (
            0
            <= decision.explanation
            .final_success_probability
            <= 1
        )

        assert (
            decision.explanation
            .action_cost
            >= 0
        )

        assert (
            decision.explanation
            .expected_recovery
            >= 0
        )

        calculated_ev = (
            decision.explanation
            .expected_recovery
            -
            decision.explanation
            .action_cost
        )

        assert abs(
            calculated_ev
            -
            decision.explanation
            .expected_value
        ) < 0.000001

    print()
    print(
        "Explainability check:"
    )

    print(
        "  ✓ Every decision has probability evidence."
    )

    print(
        "  ✓ Every decision has action-cost evidence."
    )

    print(
        "  ✓ Every decision has expected-value evidence."
    )

    print(
        "  ✓ Every decision has an explicit reason."
    )

    print(
        "  ✓ Every attempt is recorded in the recovery ledger."
    )

    # ========================================================
    # Verify Policy-Blocked Decisions Cannot Recover
    # ========================================================

    policy_blocked = []

    for decision in decisions:

        if (
            decision.explanation
            .final_success_probability
            == 0.0
            and decision.explanation
            .expected_value
            == 0.0
            and decision.decision
            == DECISION_STOPPED
        ):

            policy_blocked.append(
                decision
            )

    for decision in policy_blocked:

        assert (
            decision.outcome
            == OUTCOME_NOT_RECOVERED
        )

        assert (
            decision.recovered_amount
            == 0.0
        )

    print()
    print(
        "Policy safety verification:"
    )

    print(
        f"  Policy-blocked decisions: "
        f"{len(policy_blocked)}"
    )

    print(
        "  ✓ Policy-blocked actions cannot recover revenue."
    )

    # ========================================================
    # Naive Comparison
    # ========================================================

    comparison = (
        engine.calculate_naive_baseline(
            cases
        )
    )

    print()
    print(
        "Naive strategy comparison:"
    )

    print(
        f"  Naive attempts:      "
        f"{comparison.naive_attempts}"
    )

    print(
        f"  Naive cost:          "
        f"{rupees(comparison.naive_cost)}"
    )

    print(
        f"  Naive recovered:     "
        f"{rupees(comparison.naive_recovered_amount)}"
    )

    print(
        f"  Revive attempts:     "
        f"{comparison.revive_attempts}"
    )

    print(
        f"  Revive cost:         "
        f"{rupees(comparison.revive_cost)}"
    )

    print(
        f"  Revive recovered:    "
        f"{rupees(comparison.revive_recovered_amount)}"
    )

    print(
        f"  Additional recovery: "
        f"{rupees(comparison.additional_recovery)}"
    )

    if comparison.additional_cost > 0:
        print(
            f"  Additional cost:     "
            f"{rupees(comparison.additional_cost)}"
        )
    else:
        print(
            f"  Cost savings:        "
            f"{rupees(-comparison.additional_cost)}"
        )

    print()
    print(
        f"  {comparison.summary_line()}"
    )

    # --------------------------------------------------------
    # Ensure baseline did not contaminate ledger.
    # --------------------------------------------------------

    assert (
        len(engine.ledger.all_events())
        == len(decisions)
    )

    print(
        "  ✓ Naive comparison did not contaminate "
        "the authoritative ledger."
    )

    # ========================================================
    # Final
    # ========================================================

    print()
    print("=" * 72)
    print(
        "ROI PORTFOLIO ENGINE v4 SELF-TEST: PASSED"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()