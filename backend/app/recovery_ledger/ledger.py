"""
Revive - Recovery Ledger

Immutable event history for revenue recovery.

The ledger records what happened during recovery:

    case
      ↓
    attempt
      ↓
    action
      ↓
    cost
      ↓
    expected value
      ↓
    decision
      ↓
    outcome

IMPORTANT:

The ledger does NOT decide whether an action should happen.

That responsibility belongs to:

    Policy Engine
    ROI Engine

The ledger preserves evidence for:

    audit
    explainability
    dashboard
    future recovery decisions
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ============================================================
# Constants
# ============================================================

DECISION_PURSUED = "PURSUE"
DECISION_STOPPED = "STOP"

OUTCOME_RECOVERED = "RECOVERED"
OUTCOME_NOT_RECOVERED = "NOT_RECOVERED"
OUTCOME_BLOCKED = "BLOCKED"


# ============================================================
# Recovery Event
# ============================================================

@dataclass(frozen=True)
class RecoveryEvent:
    """
    One immutable recovery event.

    Every recovery attempt or stopping decision is represented
    by one event.
    """

    case_id: str

    attempt_number: int

    timestamp: str

    action: str

    channel: str

    amount: float

    success_probability: float

    expected_recovery: float

    action_cost: float

    expected_value: float

    decision: str

    outcome: str

    reason: str

    policy_allowed: bool = True

    policy_blocking_reasons: tuple[str, ...] = field(
        default_factory=tuple
    )


# ============================================================
# Recovery Ledger
# ============================================================

class RecoveryLedger:
    """
    In-memory append-only recovery ledger.

    The ledger does not contain recovery business logic.

    It only stores immutable recovery evidence.
    """

    def __init__(self) -> None:

        self._events: list[RecoveryEvent] = []

    # ========================================================
    # Record
    # ========================================================

    def record(
        self,
        event: RecoveryEvent,
    ) -> RecoveryEvent:
        """
        Append an event to the ledger.
        """

        if not event.case_id:

            raise ValueError(
                "case_id must not be empty."
            )

        if event.attempt_number < 1:

            raise ValueError(
                "attempt_number must be >= 1."
            )

        if event.amount < 0:

            raise ValueError(
                "amount cannot be negative."
            )

        if not 0 <= event.success_probability <= 1:

            raise ValueError(
                "success_probability must be between 0 and 1."
            )

        if event.expected_recovery < 0:

            raise ValueError(
                "expected_recovery cannot be negative."
            )

        if event.action_cost < 0:

            raise ValueError(
                "action_cost cannot be negative."
            )

        if event.decision not in {
            DECISION_PURSUED,
            DECISION_STOPPED,
        }:

            raise ValueError(
                f"Unsupported decision: {event.decision}"
            )

        if event.outcome not in {
            OUTCOME_RECOVERED,
            OUTCOME_NOT_RECOVERED,
            OUTCOME_BLOCKED,
        }:

            raise ValueError(
                f"Unsupported outcome: {event.outcome}"
            )

        if not isinstance(
            event.policy_allowed,
            bool,
        ):

            raise ValueError(
                "policy_allowed must be boolean."
            )

        # ----------------------------------------------------
        # Policy consistency
        # ----------------------------------------------------

        if not event.policy_allowed:

            if not event.policy_blocking_reasons:

                raise ValueError(
                    "A policy-blocked event must contain "
                    "policy_blocking_reasons."
                )

            if event.decision != DECISION_STOPPED:

                raise ValueError(
                    "A policy-blocked event must have "
                    "decision=STOP."
                )

            if event.action_cost != 0:

                raise ValueError(
                    "A policy-blocked event cannot incur "
                    "recovery action cost."
                )

            if event.expected_recovery != 0:

                raise ValueError(
                    "A policy-blocked event cannot have "
                    "expected recovery."
                )

        self._events.append(
            event
        )

        return event

    # ========================================================
    # All Events
    # ========================================================

    def all_events(
        self,
    ) -> list[RecoveryEvent]:
        """
        Return a copy of all events.
        """

        return list(
            self._events
        )

    # ========================================================
    # Case History
    # ========================================================

    def case_history(
        self,
        case_id: str,
    ) -> list[RecoveryEvent]:
        """
        Return chronological history for one case.
        """

        events = [
            event
            for event in self._events
            if event.case_id == case_id
        ]

        return sorted(
            events,
            key=lambda event: (
                event.attempt_number,
                event.timestamp,
            ),
        )

    # ========================================================
    # Attempt Count
    # ========================================================

    def attempt_count(
        self,
        case_id: str,
    ) -> int:

        return len(
            self.case_history(
                case_id
            )
        )

    # ========================================================
    # Last Event
    # ========================================================

    def last_event(
        self,
        case_id: str,
    ) -> RecoveryEvent | None:

        history = self.case_history(
            case_id
        )

        if not history:
            return None

        return history[-1]

    # ========================================================
    # Has Recovered
    # ========================================================

    def has_recovered(
        self,
        case_id: str,
    ) -> bool:

        return any(
            event.outcome
            == OUTCOME_RECOVERED
            for event in self.case_history(
                case_id
            )
        )

    # ========================================================
    # Total Cost
    # ========================================================

    def total_cost(
        self,
        case_id: str | None = None,
    ) -> float:

        events = (
            self.case_history(case_id)
            if case_id is not None
            else self._events
        )

        return sum(
            event.action_cost
            for event in events
            if event.decision
            == DECISION_PURSUED
        )

    # ========================================================
    # Total Recovered
    # ========================================================

    def total_recovered(
        self,
        case_id: str | None = None,
    ) -> float:

        events = (
            self.case_history(case_id)
            if case_id is not None
            else self._events
        )

        return sum(
            event.amount
            for event in events
            if event.outcome
            == OUTCOME_RECOVERED
        )

    # ========================================================
    # Pursued Events
    # ========================================================

    def pursued_events(
        self,
        case_id: str | None = None,
    ) -> list[RecoveryEvent]:

        events = (
            self.case_history(case_id)
            if case_id is not None
            else self._events
        )

        return [
            event
            for event in events
            if event.decision
            == DECISION_PURSUED
        ]

    # ========================================================
    # Stopped Events
    # ========================================================

    def stopped_events(
        self,
        case_id: str | None = None,
    ) -> list[RecoveryEvent]:

        events = (
            self.case_history(case_id)
            if case_id is not None
            else self._events
        )

        return [
            event
            for event in events
            if event.decision
            == DECISION_STOPPED
        ]

    # ========================================================
    # Recovery Rate
    # ========================================================

    def recovery_rate(
        self,
        case_id: str | None = None,
    ) -> float:

        events = (
            self.case_history(case_id)
            if case_id is not None
            else self._events
        )

        if not events:
            return 0.0

        unique_cases = {
            event.case_id
            for event in events
        }

        recovered_cases = {
            event.case_id
            for event in events
            if event.outcome
            == OUTCOME_RECOVERED
        }

        if not unique_cases:
            return 0.0

        return (
            len(recovered_cases)
            / len(unique_cases)
        )

    # ========================================================
    # Case Status
    # ========================================================

    def case_status(
        self,
        case_id: str,
    ) -> str:
        """
        Possible results:

            NO_HISTORY
            RECOVERED
            STOPPED
            ACTIVE
        """

        last = self.last_event(
            case_id
        )

        if last is None:
            return "NO_HISTORY"

        if self.has_recovered(
            case_id
        ):
            return "RECOVERED"

        if last.decision == DECISION_STOPPED:
            return "STOPPED"

        return "ACTIVE"

    # ========================================================
    # Export
    # ========================================================

    def to_dicts(
        self,
    ) -> list[dict]:

        return [

            {
                "case_id": event.case_id,

                "attempt_number": (
                    event.attempt_number
                ),

                "timestamp": event.timestamp,

                "action": event.action,

                "channel": event.channel,

                "amount": event.amount,

                "success_probability": (
                    event.success_probability
                ),

                "expected_recovery": (
                    event.expected_recovery
                ),

                "action_cost": (
                    event.action_cost
                ),

                "expected_value": (
                    event.expected_value
                ),

                "decision": event.decision,

                "outcome": event.outcome,

                "reason": event.reason,

                "policy_allowed": (
                    event.policy_allowed
                ),

                "policy_blocking_reasons": list(
                    event.policy_blocking_reasons
                ),
            }

            for event in self._events
        ]


# ============================================================
# Formatting
# ============================================================

def rupees(
    value: float,
) -> str:

    return f"₹{value:,.2f}"


# ============================================================
# Self Test
# ============================================================

def main() -> None:

    print("=" * 72)

    print("REVIVE — RECOVERY LEDGER")

    print("=" * 72)

    ledger = RecoveryLedger()

    # --------------------------------------------------------
    # TEST 1
    # --------------------------------------------------------

    print()

    print(
        "TEST 1 — Record first recovery attempt"
    )

    event_1 = RecoveryEvent(

        case_id="RV-LEDGER-001",

        attempt_number=1,

        timestamp="2026-08-29T14:00:00",

        action="whatsapp",

        channel="whatsapp",

        amount=1000.0,

        success_probability=0.35,

        expected_recovery=350.0,

        action_cost=2.0,

        expected_value=348.0,

        decision=DECISION_PURSUED,

        outcome=OUTCOME_NOT_RECOVERED,

        reason=(
            "Positive expected value; first recovery "
            "attempt is economically justified."
        ),

        policy_allowed=True,

        policy_blocking_reasons=(),
    )

    ledger.record(
        event_1
    )

    print(
        f"  Case:       {event_1.case_id}"
    )

    print(
        f"  Attempt:    #{event_1.attempt_number}"
    )

    print(
        f"  Action:     {event_1.action}"
    )

    print(
        f"  Cost:       {rupees(event_1.action_cost)}"
    )

    print(
        f"  EV:         {rupees(event_1.expected_value)}"
    )

    print(
        f"  Outcome:    {event_1.outcome}"
    )

    # --------------------------------------------------------
    # TEST 2
    # --------------------------------------------------------

    print()

    print(
        "TEST 2 — Record second attempt"
    )

    event_2 = RecoveryEvent(

        case_id="RV-LEDGER-001",

        attempt_number=2,

        timestamp="2026-08-30T14:00:00",

        action="whatsapp",

        channel="whatsapp",

        amount=1000.0,

        success_probability=0.2275,

        expected_recovery=227.50,

        action_cost=2.0,

        expected_value=225.50,

        decision=DECISION_PURSUED,

        outcome=OUTCOME_RECOVERED,

        reason=(
            "Second attempt remained economically positive."
        ),

        policy_allowed=True,

        policy_blocking_reasons=(),
    )

    ledger.record(
        event_2
    )

    history = ledger.case_history(
        "RV-LEDGER-001"
    )

    assert len(history) == 2

    assert history[0].attempt_number == 1

    assert history[1].attempt_number == 2

    print(
        f"  Recorded events: {len(history)}"
    )

    print(
        "  ✓ Chronological case history preserved."
    )

    # --------------------------------------------------------
    # TEST 3
    # --------------------------------------------------------

    print()

    print(
        "TEST 3 — Recovery status"
    )

    assert ledger.has_recovered(
        "RV-LEDGER-001"
    )

    assert (
        ledger.case_status(
            "RV-LEDGER-001"
        )
        == "RECOVERED"
    )

    print(
        "  Status:     RECOVERED"
    )

    print(
        "  ✓ Recovered cases are identified."
    )

    # --------------------------------------------------------
    # TEST 4
    # --------------------------------------------------------

    print()

    print(
        "TEST 4 — Accumulated cost"
    )

    total_cost = ledger.total_cost(
        "RV-LEDGER-001"
    )

    assert total_cost == 4.0

    print(
        f"  Total cost: {rupees(total_cost)}"
    )

    print(
        "  ✓ Cost accumulates across attempts."
    )

    # --------------------------------------------------------
    # TEST 5
    # --------------------------------------------------------

    print()

    print(
        "TEST 5 — Stopping decision"
    )

    event_3 = RecoveryEvent(

        case_id="RV-LEDGER-002",

        attempt_number=1,

        timestamp="2026-08-29T15:00:00",

        action="voice_call",

        channel="voice_call",

        amount=100.0,

        success_probability=0.01,

        expected_recovery=1.0,

        action_cost=15.0,

        expected_value=-14.0,

        decision=DECISION_STOPPED,

        outcome=OUTCOME_NOT_RECOVERED,

        reason=(
            "Expected recovery is lower than the "
            "cost of another action."
        ),

        policy_allowed=True,

        policy_blocking_reasons=(),
    )

    ledger.record(
        event_3
    )

    assert (
        ledger.case_status(
            "RV-LEDGER-002"
        )
        == "STOPPED"
    )

    stopped = ledger.stopped_events(
        "RV-LEDGER-002"
    )

    assert len(stopped) == 1

    assert (
        stopped[0].expected_value
        <= 0
    )

    print(
        "  Case:       RV-LEDGER-002"
    )

    print(
        f"  Expected value: "
        f"{rupees(event_3.expected_value)}"
    )

    print(
        "  Decision:   STOP"
    )

    print(
        "  ✓ Explicit economic stop recorded."
    )

    # --------------------------------------------------------
    # TEST 6
    # --------------------------------------------------------

    print()

    print(
        "TEST 6 — Policy-blocked event"
    )

    event_4 = RecoveryEvent(

        case_id="RV-LEDGER-003",

        attempt_number=1,

        timestamp="2026-08-30T08:14:00",

        action="whatsapp",

        channel="whatsapp",

        amount=22933.0,

        success_probability=0.0,

        expected_recovery=0.0,

        action_cost=0.0,

        expected_value=0.0,

        decision=DECISION_STOPPED,

        outcome=OUTCOME_NOT_RECOVERED,

        reason=(
            "Policy blocked action 'whatsapp'. "
            "Current time 08:14 is outside the "
            "allowed 09:00–20:00 window."
        ),

        policy_allowed=False,

        policy_blocking_reasons=(

            "Current time 08:14 is outside the "
            "allowed 09:00–20:00 window.",

        ),
    )

    ledger.record(
        event_4
    )

    assert (
        ledger.case_status(
            "RV-LEDGER-003"
        )
        == "STOPPED"
    )

    assert (
        event_4.policy_allowed
        is False
    )

    assert (
        len(
            event_4.policy_blocking_reasons
        )
        == 1
    )

    print(
        "  ✓ Policy-blocked evidence preserved."
    )

    # --------------------------------------------------------
    # TEST 7
    # --------------------------------------------------------

    print()

    print(
        "TEST 7 — No-history case"
    )

    assert (
        ledger.case_status(
            "RV-DOES-NOT-EXIST"
        )
        == "NO_HISTORY"
    )

    assert (
        ledger.last_event(
            "RV-DOES-NOT-EXIST"
        )
        is None
    )

    print(
        "  Status:     NO_HISTORY"
    )

    print(
        "  ✓ Missing history handled safely."
    )

    # --------------------------------------------------------
    # TEST 8
    # --------------------------------------------------------

    print()

    print(
        "TEST 8 — Export for API"
    )

    exported = ledger.to_dicts()

    assert len(exported) == 4

    assert exported[0]["case_id"] == (
        "RV-LEDGER-001"
    )

    assert isinstance(
        exported[0]["policy_blocking_reasons"],
        list,
    )

    assert exported[3]["policy_allowed"] is False

    assert exported[3][
        "policy_blocking_reasons"
    ]

    print(
        f"  Exported events: {len(exported)}"
    )

    print(
        "  ✓ Events are JSON-friendly."
    )

    # --------------------------------------------------------
    # TEST 9
    # --------------------------------------------------------

    print()

    print(
        "TEST 9 — Validation"
    )

    try:

        ledger.record(

            RecoveryEvent(

                case_id="RV-BAD",

                attempt_number=0,

                timestamp="2026-08-29T16:00:00",

                action="email",

                channel="email",

                amount=100.0,

                success_probability=0.2,

                expected_recovery=20.0,

                action_cost=0.5,

                expected_value=19.5,

                decision=DECISION_PURSUED,

                outcome=OUTCOME_NOT_RECOVERED,

                reason="Invalid attempt number.",
            )
        )

        raise AssertionError(
            "Invalid attempt number was accepted."
        )

    except ValueError:

        print(
            "  ✓ Invalid events are rejected."
        )

    # --------------------------------------------------------
    # Final Metrics
    # --------------------------------------------------------

    print()

    print(
        "Ledger metrics:"
    )

    print(
        f"  Total events:      "
        f"{len(ledger.all_events())}"
    )

    print(
        f"  Total cost:        "
        f"{rupees(ledger.total_cost())}"
    )

    print(
        f"  Total recovered:   "
        f"{rupees(ledger.total_recovered())}"
    )

    print(
        f"  Recovery rate:     "
        f"{ledger.recovery_rate():.2%}"
    )

    print(
        f"  Stopped events:    "
        f"{len(ledger.stopped_events())}"
    )

    print()

    print("=" * 72)

    print(
        "RECOVERY LEDGER SELF-TEST: PASSED"
    )

    print("=" * 72)


if __name__ == "__main__":
    main()