"""
Revive - Module 3
Policy & Compliance Engine

The Policy Engine is the deterministic guardrail layer of Revive.

Architecture:

    AI proposes action
            |
            v
    Policy Engine
            |
       +----+----+
       |         |
    ALLOWED    BLOCKED
       |         |
       v         v
    Execute    Log reason

IMPORTANT:
    The policy engine must never depend on an LLM.

Business rules are loaded from policy.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


# ============================================================
# Paths
# ============================================================

CURRENT_DIR = Path(__file__).resolve().parent

POLICY_FILE = CURRENT_DIR / "policy.yaml"


# ============================================================
# Data classes
# ============================================================

@dataclass
class CaseState:
    """
    Runtime state associated with a revenue recovery case.

    This state will later be extended by:

        Module 5 — Promise-to-Pay
        Module 6 — ROI Engine
        Module 7 — A2A Settlement
    """

    case_id: str

    contact_attempts: int = 0

    last_contact_at: datetime | None = None

    promise_to_pay_active: bool = False

    promise_date: datetime | None = None

    disputed: bool = False

    opted_out: bool = False

    negotiation_rounds: int = 0

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class PolicyCheck:
    """
    Individual policy check.

    Every check is preserved so the final decision
    is explainable.
    """

    name: str

    passed: bool

    reason: str


@dataclass(frozen=True)
class PolicyCheckResult:
    """
    Complete policy decision.

    `allowed` is the final result.

    `checks` contains every individual rule evaluation.

    `blocking_reasons` contains every reason that prevented
    the action.
    """

    case_id: str

    action: str

    allowed: bool

    checks: list[PolicyCheck]

    blocking_reasons: list[str]


# ============================================================
# Policy loader
# ============================================================

def load_policy() -> dict[str, Any]:
    """
    Load policy configuration from YAML.
    """

    if not POLICY_FILE.exists():
        raise FileNotFoundError(
            f"Policy file not found: {POLICY_FILE}"
        )

    with POLICY_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:

        policy = yaml.safe_load(file)

    if not isinstance(policy, dict):
        raise ValueError(
            "policy.yaml must contain a YAML mapping."
        )

    return policy


# ============================================================
# Policy Engine
# ============================================================

class PolicyEngine:
    """
    Deterministic policy and compliance engine.

    No LLM decisions are made here.
    """

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
    ) -> None:

        self.policy = (
            policy
            if policy is not None
            else load_policy()
        )

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    def _check_contact_limit(
        self,
        state: CaseState,
    ) -> PolicyCheck:

        limit = int(
            self.policy[
                "max_contact_attempts"
            ]
        )

        passed = (
            state.contact_attempts
            < limit
        )

        return PolicyCheck(
            name="contact_limit",
            passed=passed,
            reason=(
                f"Contact attempts: "
                f"{state.contact_attempts}/{limit}."
                if passed
                else
                f"Maximum contact attempts "
                f"reached: {limit}."
            ),
        )

    # --------------------------------------------------------

    def _check_cooldown(
        self,
        state: CaseState,
        now: datetime,
    ) -> PolicyCheck:

        cooldown_hours = float(
            self.policy[
                "cooldown_hours"
            ]
        )

        if state.last_contact_at is None:

            return PolicyCheck(
                name="cooldown",
                passed=True,
                reason=(
                    "No previous contact exists; "
                    "cooldown does not apply."
                ),
            )

        elapsed = (
            now - state.last_contact_at
        )

        required = timedelta(
            hours=cooldown_hours
        )

        passed = elapsed >= required

        if passed:

            reason = (
                f"Cooldown expired. "
                f"Elapsed: {elapsed}."
            )

        else:

            remaining = (
                required - elapsed
            )

            reason = (
                f"Cooldown active. "
                f"Remaining: {remaining}."
            )

        return PolicyCheck(
            name="cooldown",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------

    def _check_contact_window(
        self,
        now: datetime,
    ) -> PolicyCheck:

        window = self.policy[
            "contact_window"
        ]

        start_text = window["start"]
        end_text = window["end"]

        start_hour, start_minute = map(
            int,
            start_text.split(":"),
        )

        end_hour, end_minute = map(
            int,
            end_text.split(":"),
        )

        current_minutes = (
            now.hour * 60
            + now.minute
        )

        start_minutes = (
            start_hour * 60
            + start_minute
        )

        end_minutes = (
            end_hour * 60
            + end_minute
        )

        passed = (
            start_minutes
            <= current_minutes
            <= end_minutes
        )

        if passed:

            reason = (
                f"Current time {now.strftime('%H:%M')} "
                f"is inside the allowed "
                f"{start_text}–{end_text} window."
            )

        else:

            reason = (
                f"Current time {now.strftime('%H:%M')} "
                f"is outside the allowed "
                f"{start_text}–{end_text} window."
            )

        return PolicyCheck(
            name="contact_window",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------

    def _check_promise_to_pay(
        self,
        state: CaseState,
    ) -> PolicyCheck:

        enabled = bool(
            self.policy[
                "hard_stops"
            ][
                "promise_to_pay"
            ]
        )

        if not enabled:

            return PolicyCheck(
                name="promise_to_pay",
                passed=True,
                reason=(
                    "Promise-to-pay hard stop "
                    "is disabled."
                ),
            )

        passed = not state.promise_to_pay_active

        if passed:

            reason = (
                "No active promise-to-pay exists."
            )

        else:

            if state.promise_date:

                reason = (
                    "Active promise-to-pay exists "
                    f"until {state.promise_date.isoformat()}."
                )

            else:

                reason = (
                    "Active promise-to-pay exists."
                )

        return PolicyCheck(
            name="promise_to_pay",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------

    def _check_dispute(
        self,
        state: CaseState,
    ) -> PolicyCheck:

        enabled = bool(
            self.policy[
                "hard_stops"
            ][
                "disputed_invoice"
            ]
        )

        if not enabled:

            return PolicyCheck(
                name="disputed_invoice",
                passed=True,
                reason=(
                    "Disputed-invoice hard stop "
                    "is disabled."
                ),
            )

        passed = not state.disputed

        if passed:

            reason = (
                "Invoice is not marked as disputed."
            )

        else:

            reason = (
                "Invoice is disputed; automated "
                "recovery is prohibited."
            )

        return PolicyCheck(
            name="disputed_invoice",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------

    def _check_opt_out(
        self,
        state: CaseState,
    ) -> PolicyCheck:

        enabled = bool(
            self.policy[
                "hard_stops"
            ][
                "customer_opted_out"
            ]
        )

        if not enabled:

            return PolicyCheck(
                name="customer_opted_out",
                passed=True,
                reason=(
                    "Customer opt-out hard stop "
                    "is disabled."
                ),
            )

        passed = not state.opted_out

        if passed:

            reason = (
                "Customer has not opted out."
            )

        else:

            reason = (
                "Customer has opted out; automated "
                "contact is prohibited."
            )

        return PolicyCheck(
            name="customer_opted_out",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------

    def _check_discount(
        self,
        discount_percent: float,
    ) -> PolicyCheck:

        maximum = float(
            self.policy[
                "max_discount_percent"
            ]
        )

        passed = (
            discount_percent
            <= maximum
        )

        if passed:

            reason = (
                f"Requested discount "
                f"{discount_percent:.2f}% "
                f"is within the {maximum:.2f}% cap."
            )

        else:

            reason = (
                f"Requested discount "
                f"{discount_percent:.2f}% "
                f"exceeds the {maximum:.2f}% cap."
            )

        return PolicyCheck(
            name="discount_limit",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------

    def _check_negotiation_rounds(
        self,
        state: CaseState,
    ) -> PolicyCheck:

        maximum = int(
            self.policy[
                "max_negotiation_rounds"
            ]
        )

        passed = (
            state.negotiation_rounds
            < maximum
        )

        if passed:

            reason = (
                f"Negotiation rounds: "
                f"{state.negotiation_rounds}/{maximum}."
            )

        else:

            reason = (
                f"Maximum negotiation rounds "
                f"reached: {maximum}."
            )

        return PolicyCheck(
            name="negotiation_round_limit",
            passed=passed,
            reason=reason,
        )

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def check_action(
        self,
        state: CaseState,
        action: str,
        now: datetime | None = None,
        discount_percent: float = 0.0,
    ) -> PolicyCheckResult:

        if now is None:
            now = datetime.now()

        checks: list[PolicyCheck] = []

        # ----------------------------------------------------
        # Universal checks for customer contact
        # ----------------------------------------------------

        contact_actions = {
            "whatsapp",
            "email",
            "voice_call",
        }

        if action in contact_actions:

            checks.append(
                self._check_contact_limit(
                    state
                )
            )

            checks.append(
                self._check_cooldown(
                    state,
                    now,
                )
            )

            checks.append(
                self._check_contact_window(
                    now
                )
            )

            checks.append(
                self._check_promise_to_pay(
                    state
                )
            )

            checks.append(
                self._check_dispute(
                    state
                )
            )

            checks.append(
                self._check_opt_out(
                    state
                )
            )

        # ----------------------------------------------------
        # Payment retry
        # ----------------------------------------------------

        elif action == "payment_retry":

            retry_config = self.policy[
                "retry"
            ]

            maximum = int(
                retry_config[
                    "max_attempts"
                ]
            )

            passed = (
                state.contact_attempts
                < maximum
            )

            checks.append(
                PolicyCheck(
                    name="retry_limit",
                    passed=passed,
                    reason=(
                        f"Retry attempts: "
                        f"{state.contact_attempts}/{maximum}."
                        if passed
                        else
                        f"Maximum retry attempts "
                        f"reached: {maximum}."
                    ),
                )
            )

            checks.append(
                self._check_promise_to_pay(
                    state
                )
            )

            checks.append(
                self._check_dispute(
                    state
                )
            )

        # ----------------------------------------------------
        # Negotiation
        # ----------------------------------------------------

        elif action == "negotiate":

            checks.append(
                self._check_negotiation_rounds(
                    state
                )
            )

            checks.append(
                self._check_promise_to_pay(
                    state
                )
            )

            checks.append(
                self._check_dispute(
                    state
                )
            )

            checks.append(
                self._check_opt_out(
                    state
                )
            )

            checks.append(
                self._check_discount(
                    discount_percent
                )
            )

        # ----------------------------------------------------
        # Human escalation
        # ----------------------------------------------------

        elif action == "human_escalation":

            checks.append(
                PolicyCheck(
                    name="escalation",
                    passed=bool(
                        self.policy[
                            "escalation"
                        ][
                            "enabled"
                        ]
                    ),
                    reason=(
                        "Human escalation is enabled."
                        if self.policy[
                            "escalation"
                        ][
                            "enabled"
                        ]
                        else
                        "Human escalation is disabled."
                    ),
                )
            )

        # ----------------------------------------------------
        # STOP is always permitted.
        # ----------------------------------------------------

        elif action == "stop":

            checks.append(
                PolicyCheck(
                    name="stop_action",
                    passed=True,
                    reason=(
                        "Stopping automated recovery "
                        "is always permitted."
                    ),
                )
            )

        # ----------------------------------------------------
        # Unknown action
        # ----------------------------------------------------

        else:

            checks.append(
                PolicyCheck(
                    name="known_action",
                    passed=False,
                    reason=(
                        f"Unknown action: {action}"
                    ),
                )
            )

        blocking_reasons = [
            check.reason
            for check in checks
            if not check.passed
        ]

        allowed = (
            len(blocking_reasons) == 0
        )

        return PolicyCheckResult(
            case_id=state.case_id,
            action=action,
            allowed=allowed,
            checks=checks,
            blocking_reasons=blocking_reasons,
        )


# ============================================================
# Self-test
# ============================================================

def main() -> None:

    print("=" * 70)
    print("REVIVE — MODULE 3")
    print("Policy & Compliance Engine")
    print("=" * 70)

    engine = PolicyEngine()

    print()
    print("Policy loaded from:")
    print(f"  {POLICY_FILE}")

    # --------------------------------------------------------
    # Test 1 — Normal allowed contact
    # --------------------------------------------------------

    normal_state = CaseState(
        case_id="RV-POLICY-001",
        contact_attempts=0,
    )

    test_time = datetime(
        2026,
        8,
        29,
        14,
        30,
    )

    result = engine.check_action(
        state=normal_state,
        action="whatsapp",
        now=test_time,
    )

    print()
    print("TEST 1 — Normal WhatsApp contact")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is True

    # --------------------------------------------------------
    # Test 2 — Promise-to-pay hard stop
    # --------------------------------------------------------

    promise_state = CaseState(
        case_id="RV-POLICY-002",
        contact_attempts=1,
        promise_to_pay_active=True,
        promise_date=datetime(
            2026,
            9,
            2,
            12,
            0,
        ),
    )

    result = engine.check_action(
        state=promise_state,
        action="whatsapp",
        now=test_time,
    )

    print()
    print("TEST 2 — Active promise-to-pay")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    assert any(
        "Active promise-to-pay"
        in reason
        for reason in result.blocking_reasons
    )

    # --------------------------------------------------------
    # Test 3 — Contact limit
    # --------------------------------------------------------

    limit_state = CaseState(
        case_id="RV-POLICY-003",
        contact_attempts=3,
    )

    result = engine.check_action(
        state=limit_state,
        action="email",
        now=test_time,
    )

    print()
    print("TEST 3 — Contact limit")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    # --------------------------------------------------------
    # Test 4 — Outside contact window
    # --------------------------------------------------------

    outside_window_state = CaseState(
        case_id="RV-POLICY-004",
        contact_attempts=0,
    )

    outside_time = datetime(
        2026,
        8,
        29,
        22,
        0,
    )

    result = engine.check_action(
        state=outside_window_state,
        action="whatsapp",
        now=outside_time,
    )

    print()
    print("TEST 4 — Outside contact window")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    # --------------------------------------------------------
    # Test 5 — Discount within limit
    # --------------------------------------------------------

    negotiation_state = CaseState(
        case_id="RV-POLICY-005",
        negotiation_rounds=0,
    )

    result = engine.check_action(
        state=negotiation_state,
        action="negotiate",
        now=test_time,
        discount_percent=5,
    )

    print()
    print("TEST 5 — Negotiation within policy")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is True

    # --------------------------------------------------------
    # Test 6 — Discount exceeds limit
    # --------------------------------------------------------

    result = engine.check_action(
        state=negotiation_state,
        action="negotiate",
        now=test_time,
        discount_percent=15,
    )

    print()
    print("TEST 6 — Negotiation exceeds discount cap")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    assert any(
        "exceeds"
        in reason
        for reason in result.blocking_reasons
    )

    # --------------------------------------------------------
    # Test 7 — Disputed invoice
    # --------------------------------------------------------

    disputed_state = CaseState(
        case_id="RV-POLICY-007",
        disputed=True,
    )

    result = engine.check_action(
        state=disputed_state,
        action="voice_call",
        now=test_time,
    )

    print()
    print("TEST 7 — Disputed invoice")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    # --------------------------------------------------------
    # Test 8 — Opted-out customer
    # --------------------------------------------------------

    opted_out_state = CaseState(
        case_id="RV-POLICY-008",
        opted_out=True,
    )

    result = engine.check_action(
        state=opted_out_state,
        action="email",
        now=test_time,
    )

    print()
    print("TEST 8 — Customer opted out")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    # --------------------------------------------------------
    # Test 9 — Cooldown
    # --------------------------------------------------------

    cooldown_state = CaseState(
        case_id="RV-POLICY-009",
        contact_attempts=1,
        last_contact_at=datetime(
            2026,
            8,
            29,
            13,
            0,
        ),
    )

    result = engine.check_action(
        state=cooldown_state,
        action="whatsapp",
        now=datetime(
            2026,
            8,
            29,
            13,
            30,
        ),
    )

    print()
    print("TEST 9 — Active cooldown")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is False

    # --------------------------------------------------------
    # Test 10 — Stop action
    # --------------------------------------------------------

    stop_state = CaseState(
        case_id="RV-POLICY-010",
        contact_attempts=3,
        opted_out=True,
        disputed=True,
    )

    result = engine.check_action(
        state=stop_state,
        action="stop",
        now=test_time,
    )

    print()
    print("TEST 10 — STOP action")

    for check in result.checks:
        symbol = "✓" if check.passed else "✗"

        print(
            f"  {symbol} "
            f"{check.name}: "
            f"{check.reason}"
        )

    print(
        f"  DECISION: "
        f"{'ALLOWED' if result.allowed else 'BLOCKED'}"
    )

    assert result.allowed is True

    # --------------------------------------------------------
    # Final result
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("MODULE 3 SELF-TEST: PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()