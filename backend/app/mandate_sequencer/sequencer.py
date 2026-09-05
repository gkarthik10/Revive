"""
Revive - Mandate Retry Sequencer

Standalone module for sequencing retries on failed UPI Autopay /
eNACH recurring-mandate debits.

Why this is its own module and not just another `retry` case:

    A generic card retry and a mandate debit retry are governed by
    different real-world constraints:

        - A pre-debit notice must reach the customer before each
          retry attempt (not just before the first debit).
        - Attempts are capped per billing cycle, not just per case
          lifetime — the cap resets when the next cycle starts.
        - The minimum spacing between attempts is materially wider
          than a card retry's cooldown.
        - Once the per-cycle cap is exhausted, the correct next
          step is a bounded re-authorization request — not another
          silent debit attempt.

    This module only applies to `mandate_debit_failed`: the mandate
    itself is still active and only a single debit attempt failed
    (e.g. insufficient balance on the debit date). It intentionally
    does NOT apply to `mandate_expired_or_revoked`, where the
    mandate is already dead and retrying a debit against it would
    itself be a compliance problem — that root cause goes straight
    to a re-authorization request (see orchestrator.py).

Scope:
    This module produces a *plan* (a proposed, policy-bounded
    schedule) and an *audit record* explaining every stopping rule
    that was applied. It does not send anything and does not decide
    final recovery amounts — those remain owned by the ROI engine
    and the Policy Engine, respectively. This keeps a single source
    of authority: the sequencer answers "what is a compliant
    sequence of mandate retries", nothing more.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


# ============================================================
# Paths
# ============================================================

CURRENT_DIR = Path(__file__).resolve().parent

POLICY_FILE = (
    CURRENT_DIR.parent / "core" / "policy.yaml"
)

APPLICABLE_ROOT_CAUSE = "mandate_debit_failed"


# ============================================================
# Configuration
# ============================================================

@dataclass(frozen=True)
class MandateRetryConfig:
    """
    Loaded from policy.yaml's `mandate_retry` block.

    Kept as its own dataclass (rather than reading raw dict keys
    all over this module) so a missing/invalid config key fails
    loudly, once, at load time.
    """

    max_attempts_per_cycle: int
    minimum_gap_hours: int
    predebit_notice_hours: int
    billing_cycle_days: int
    escalation_action: str

    def __post_init__(self) -> None:

        if self.max_attempts_per_cycle < 1:
            raise ValueError(
                "mandate_retry.max_attempts_per_cycle must be >= 1."
            )

        if self.minimum_gap_hours < 1:
            raise ValueError(
                "mandate_retry.minimum_gap_hours must be >= 1."
            )

        if self.predebit_notice_hours < 1:
            raise ValueError(
                "mandate_retry.predebit_notice_hours must be >= 1."
            )

        if self.billing_cycle_days < 1:
            raise ValueError(
                "mandate_retry.billing_cycle_days must be >= 1."
            )

        if not self.escalation_action:
            raise ValueError(
                "mandate_retry.escalation_action must be set."
            )


def load_mandate_retry_config(
    policy: dict[str, Any] | None = None,
) -> MandateRetryConfig:
    """
    Load the mandate_retry block from policy.yaml (or from an
    already-loaded policy dict, e.g. a what-if override).
    """

    if policy is None:
        with POLICY_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            policy = yaml.safe_load(file)

    if "mandate_retry" not in policy:
        raise KeyError(
            "policy.yaml is missing the required "
            "'mandate_retry' block."
        )

    block = policy["mandate_retry"]

    return MandateRetryConfig(
        max_attempts_per_cycle=int(
            block["max_attempts_per_cycle"]
        ),
        minimum_gap_hours=int(
            block["minimum_gap_hours"]
        ),
        predebit_notice_hours=int(
            block["predebit_notice_hours"]
        ),
        billing_cycle_days=int(
            block["billing_cycle_days"]
        ),
        escalation_action=str(
            block["escalation_action"]
        ),
    )


# ============================================================
# Plan data classes
# ============================================================

@dataclass(frozen=True)
class ScheduledAttempt:
    """One proposed debit retry within the current billing cycle."""

    attempt_number: int
    notify_at: datetime
    retry_at: datetime
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "notify_at": self.notify_at.isoformat(),
            "retry_at": self.retry_at.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MandateRetryPlan:
    """
    Full compliant retry plan for one mandate_debit_failed case.

    `escalate` is True once the per-cycle attempt cap has been
    reached in this plan — at that point the correct action is
    `escalation_action` (mandate re-authorization), not a further
    debit attempt. This is the sequencer's stopping rule.
    """

    case_id: str
    root_cause: str
    cycle_started_at: datetime
    cycle_ends_at: datetime
    attempts: tuple[ScheduledAttempt, ...]
    escalate: bool
    escalation_action: str | None
    audit_trail: tuple[str, ...] = field(
        default_factory=tuple
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "root_cause": self.root_cause,
            "cycle_started_at": (
                self.cycle_started_at.isoformat()
            ),
            "cycle_ends_at": self.cycle_ends_at.isoformat(),
            "attempts": [
                attempt.to_dict()
                for attempt in self.attempts
            ],
            "escalate": self.escalate,
            "escalation_action": self.escalation_action,
            "audit_trail": list(self.audit_trail),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            indent=2,
        )


# ============================================================
# Sequencer
# ============================================================

class MandateRetrySequencer:
    """
    Builds a compliant retry schedule for a single
    `mandate_debit_failed` case.

    Deterministic: given the same case, config, prior attempt
    count and `now`, always produces the same plan. No randomness,
    no network calls, no side effects — safe to call repeatedly
    for audit / what-if purposes.
    """

    def __init__(
        self,
        config: MandateRetryConfig | None = None,
    ) -> None:

        self.config = (
            config
            if config is not None
            else load_mandate_retry_config()
        )

    def build_plan(
        self,
        case: dict[str, Any],
        prior_attempts_this_cycle: int = 0,
        now: datetime | None = None,
    ) -> MandateRetryPlan:

        root_cause = case.get("root_cause_label") or case.get(
            "decline_code"
        )

        if root_cause != APPLICABLE_ROOT_CAUSE:
            raise ValueError(
                "MandateRetrySequencer only applies to "
                f"'{APPLICABLE_ROOT_CAUSE}' cases; got "
                f"'{root_cause}' for case "
                f"{case.get('case_id')}."
            )

        if prior_attempts_this_cycle < 0:
            raise ValueError(
                "prior_attempts_this_cycle cannot be negative."
            )

        if now is None:
            now = datetime.fromisoformat(
                case["timestamp"]
            )

        cycle_started_at = now
        cycle_ends_at = now + timedelta(
            days=self.config.billing_cycle_days
        )

        audit_trail: list[str] = [
            f"Root cause '{root_cause}' confirmed applicable to "
            "the mandate retry sequencer (mandate still active; "
            "only the debit attempt failed).",
            (
                f"Billing cycle window: {cycle_started_at.isoformat()} "
                f"to {cycle_ends_at.isoformat()} "
                f"({self.config.billing_cycle_days} days)."
            ),
            (
                f"Per-cycle attempt cap: "
                f"{self.config.max_attempts_per_cycle}. "
                f"Prior attempts already used this cycle: "
                f"{prior_attempts_this_cycle}."
            ),
        ]

        remaining_attempts = (
            self.config.max_attempts_per_cycle
            - prior_attempts_this_cycle
        )

        if remaining_attempts <= 0:

            audit_trail.append(
                "Stopping rule triggered: per-cycle attempt cap "
                "already reached. No further debit attempt is "
                "compliant this cycle — escalating to "
                f"'{self.config.escalation_action}' instead."
            )

            return MandateRetryPlan(
                case_id=case["case_id"],
                root_cause=root_cause,
                cycle_started_at=cycle_started_at,
                cycle_ends_at=cycle_ends_at,
                attempts=tuple(),
                escalate=True,
                escalation_action=self.config.escalation_action,
                audit_trail=tuple(audit_trail),
            )

        attempts: list[ScheduledAttempt] = []

        cursor = now

        for offset in range(remaining_attempts):

            attempt_number = (
                prior_attempts_this_cycle + offset + 1
            )

            if offset == 0:
                retry_at = cursor + timedelta(
                    hours=self.config.predebit_notice_hours
                )
            else:
                retry_at = cursor + timedelta(
                    hours=max(
                        self.config.minimum_gap_hours,
                        self.config.predebit_notice_hours,
                    )
                )

            notify_at = retry_at - timedelta(
                hours=self.config.predebit_notice_hours
            )

            reason = (
                f"Attempt #{attempt_number} of "
                f"{self.config.max_attempts_per_cycle} this "
                "cycle. Pre-debit notice sent "
                f"{self.config.predebit_notice_hours}h before "
                "the retry, honoring the minimum "
                f"{self.config.minimum_gap_hours}h gap from the "
                "previous attempt."
            )

            attempts.append(
                ScheduledAttempt(
                    attempt_number=attempt_number,
                    notify_at=notify_at,
                    retry_at=retry_at,
                    reason=reason,
                )
            )

            cursor = retry_at

        will_exhaust_cap = (
            prior_attempts_this_cycle + len(attempts)
            >= self.config.max_attempts_per_cycle
        )

        # `escalate` only signals that the per-cycle cap has
        # already been exhausted *before* this call (the branch
        # above, `remaining_attempts <= 0`). Here the cap is not
        # yet exhausted — we are actively scheduling the attempts
        # that will use it up — so this plan is still a retry
        # plan, not an escalation. We only note in the audit
        # trail that no further attempt should be scheduled after
        # these if they all fail.
        escalate = False
        escalation_action = None

        if will_exhaust_cap:

            audit_trail.append(
                "This plan's final scheduled attempt exhausts "
                "the per-cycle cap. If it also fails, the next "
                f"call to the sequencer will escalate to "
                f"'{self.config.escalation_action}' — no further "
                "debit attempt is compliant this cycle."
            )

        audit_trail.append(
            f"Scheduled {len(attempts)} compliant retry "
            "attempt(s), each preceded by a pre-debit notice."
        )

        return MandateRetryPlan(
            case_id=case["case_id"],
            root_cause=root_cause,
            cycle_started_at=cycle_started_at,
            cycle_ends_at=cycle_ends_at,
            attempts=tuple(attempts),
            escalate=escalate,
            escalation_action=escalation_action,
            audit_trail=tuple(audit_trail),
        )


# ============================================================
# Self-test
# ============================================================

def _run_self_test() -> None:

    print("=" * 70)
    print("REVIVE — MANDATE RETRY SEQUENCER")
    print("Self-Test")
    print("=" * 70)

    config = MandateRetryConfig(
        max_attempts_per_cycle=3,
        minimum_gap_hours=24,
        predebit_notice_hours=24,
        billing_cycle_days=30,
        escalation_action="mandate_reauthorization_request",
    )

    sequencer = MandateRetrySequencer(config=config)

    now = datetime(2026, 9, 1, 9, 0, 0)

    sample_case = {
        "case_id": "RV-TEST-001",
        "root_cause_label": "mandate_debit_failed",
        "timestamp": now.isoformat(),
    }

    checks_passed = 0

    # 1. A fresh case gets a full schedule, not an escalation.
    plan = sequencer.build_plan(
        sample_case,
        prior_attempts_this_cycle=0,
        now=now,
    )
    assert not plan.escalate, (
        "A fresh mandate_debit_failed case should not "
        "escalate immediately."
    )
    checks_passed += 1

    # 2. Attempts never exceed the per-cycle cap.
    assert len(plan.attempts) <= config.max_attempts_per_cycle
    checks_passed += 1

    # 3. Every attempt is preceded by the required notice window.
    for attempt in plan.attempts:
        gap = (
            attempt.retry_at - attempt.notify_at
        ).total_seconds() / 3600
        assert gap == config.predebit_notice_hours, (
            "Pre-debit notice window was not honored."
        )
    checks_passed += 1

    # 4. Attempts are spaced at least minimum_gap_hours apart.
    for earlier, later in zip(
        plan.attempts,
        plan.attempts[1:],
    ):
        gap_hours = (
            later.retry_at - earlier.retry_at
        ).total_seconds() / 3600
        assert gap_hours >= config.minimum_gap_hours, (
            "Minimum gap between mandate retry attempts "
            "was violated."
        )
    checks_passed += 1

    # 5. Once the cap is already used up, the sequencer escalates
    #    instead of scheduling another debit attempt.
    exhausted_plan = sequencer.build_plan(
        sample_case,
        prior_attempts_this_cycle=config.max_attempts_per_cycle,
        now=now,
    )
    assert exhausted_plan.escalate
    assert exhausted_plan.attempts == tuple()
    assert (
        exhausted_plan.escalation_action
        == config.escalation_action
    )
    checks_passed += 1

    # 6. The sequencer refuses to run on the wrong root cause —
    #    it must not silently retry a debit against a dead mandate.
    wrong_cause_case = dict(sample_case)
    wrong_cause_case["root_cause_label"] = (
        "mandate_expired_or_revoked"
    )
    try:
        sequencer.build_plan(wrong_cause_case, now=now)
        raised = False
    except ValueError:
        raised = True
    assert raised, (
        "Sequencer must reject root causes other than "
        "mandate_debit_failed."
    )
    checks_passed += 1

    # 7. The plan is JSON-serializable for the audit trail.
    serialized = plan.to_json()
    parsed = json.loads(serialized)
    assert parsed["case_id"] == "RV-TEST-001"
    assert isinstance(parsed["attempts"], list)
    checks_passed += 1

    print()
    print(f"Checks passed: {checks_passed}/7")
    print()
    print("Sample plan:")
    print(plan.to_json())

    assert checks_passed == 7

    print()
    print("=" * 70)
    print("MANDATE RETRY SEQUENCER SELF-TEST: PASSED")
    print("=" * 70)


if __name__ == "__main__":
    _run_self_test()
