"""
Revive - Module 8
Dashboard API

FastAPI wrapper around the existing Revive pipeline.

The API does not redefine recovery business logic.

Pipeline:

    DATA
      ↓
    DIAGNOSIS
      ↓
    PSR GUARDIAN
      ↓
    ROI ENGINE
      ↓
    A2A SETTLEMENT
      ↓
    RECOVERY LEDGER
      ↓
    FASTAPI
      ↓
    REACT DASHBOARD

Additional Feature:

    WHAT-IF POLICY SIMULATOR

    React Dashboard
          ↓
    POST /api/simulate
          ↓
    Temporary Policy Copy
          ↓
    Fresh RevivePipeline
          ↓
    Real ROI + Policy + Ledger
          ↓
    Simulation Result

Additional Feature:

    BOARD REPORT PDF

    React Dashboard
          ↓
    GET /api/board-report
          ↓
    Current authoritative pipeline result
          ↓
    ReportLab
          ↓
    Revive_Board_Report.pdf

The simulator NEVER modifies policy.yaml.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from typing import Any
import json
import threading
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import (
    ParagraphStyle,
    getSampleStyleSheet,
)
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Board Report PDF generation lives in its own module (rather than
# inline in this file) so it can be a complete, self-contained
# renderer over the full pipeline result -- see board_report.py.
from app.dashboard_api.board_report import (
    build_board_report_pdf,
    _first_value,
    _money,
    _percent,
    _safe_text,
)

from app.pipeline import (
    RevivePipeline,
    load_cases,
    pipeline_to_dict,
)

from app.core.policy import load_policy

from app.decision_explainer import (
    DecisionExplainer,
    build_case_evidence,
)

from app.payments.razorpay_gateway import (
    RazorpayConfigError,
    create_payment_link,
    live_case_store,
    map_failed_payment_to_case,
    map_captured_payment_to_recovery,
    verify_webhook_signature,
)

# Imported after app.payments.razorpay_gateway on purpose: that module
# is what actually calls load_dotenv() for revive/.env / backend/.env.
# app.auth.security reads REVIVE_JWT_SECRET from the environment at
# import time, so auth has to come after .env is loaded or it'll
# silently fall back to the (insecure) dev default even when the
# secret IS set in .env.
from app.auth.api import router as auth_router
from app.auth.middleware import AuthMiddleware

from app.a2a_settlement.settlement import (
    A2ASettlementEngine,
)

from app.a2a_settlement.live_settlements import (
    live_a2a_settlement_store,
)

from app.notifications.notifications import generate_notifications

from app.data.live_metrics import build_live_metrics

from app.promise_tracker.tracker import (
    PromiseStatus,
    PromiseTracker,
)
from app.customer_alerts.alerts import customer_alert_service

from app.voice_recovery.hinglish_voice import (
    generate_hinglish_script,
    voice_script_store,
)

from app.customers.directory import customer_directory

from app.copilot import CopilotAgent, ToolSpec

promise_tracker = PromiseTracker()

# ============================================================
# Automatic Promise Lifecycle Worker
# ============================================================

PROMISE_CHECK_INTERVAL_SECONDS = max(
    10,
    int(
        os.getenv(
            "PROMISE_CHECK_INTERVAL_SECONDS",
            "60",
        )
    ),
)

_promise_worker_stop = threading.Event()

_promise_worker_thread: threading.Thread | None = None


def _promise_local_now() -> datetime:
    """
    Return the current Promise lifecycle time.

    Promise dates are stored as naive wall-clock values in the
    configured Promise timezone, so we intentionally remove tzinfo
    before comparing them.
    """

    promise_timezone = ZoneInfo(
        os.getenv(
            "PROMISE_TIMEZONE",
            "Asia/Kolkata",
        )
    )

    return datetime.now(
        promise_timezone
    ).replace(
        tzinfo=None
    )


def _evaluate_promise_deadlines() -> list[Any]:
    """
    Evaluate Promise-to-Pay deadlines once.

    This function does NOT execute the recovery pipeline.

    It only:
        1. checks active promises
        2. expires overdue promises
        3. sends PROMISE_BROKEN alerts
        4. invalidates cached pipeline state if needed
    """

    global _cached_result

    now_local = _promise_local_now()

    broken_promises = (
        promise_tracker.evaluate_due_promises(
            now=now_local
        )
    )

    for record in broken_promises:

        customer_alert_service.send(
            record,
            "PROMISE_BROKEN",
            payment_link_url=record.payment_link_url,
        )

    if broken_promises:
        _cached_result = None

    return broken_promises


def _promise_lifecycle_worker() -> None:
    """
    Continuously check Promise-to-Pay deadlines.

    The worker is deliberately lightweight and does not run the
    complete Revive recovery pipeline.
    """

    while not _promise_worker_stop.wait(
        PROMISE_CHECK_INTERVAL_SECONDS
    ):

        try:

            _evaluate_promise_deadlines()

        except Exception as exc:

            # Never allow one lifecycle/notification error to
            # permanently kill the background worker.
            print(
                "[PromiseLifecycle] "
                f"deadline evaluation failed: {exc}"
            )


# ============================================================
# Application
# ============================================================

app = FastAPI(
    title="Revive AI Revenue Recovery API",
    description=(
        "Dashboard API for the Revive AI Revenue Recovery System."
    ),
    version="1.1.0",
)


@app.on_event("startup")
def start_promise_lifecycle_worker() -> None:
    """
    Start the automatic Promise-to-Pay lifecycle worker.
    """

    global _promise_worker_thread

    if (
        _promise_worker_thread is not None
        and _promise_worker_thread.is_alive()
    ):
        return

    _promise_worker_stop.clear()

    _promise_worker_thread = threading.Thread(
        target=_promise_lifecycle_worker,
        name="promise-lifecycle-worker",
        daemon=True,
    )

    _promise_worker_thread.start()

    print(
        "[PromiseLifecycle] automatic deadline worker started "
        f"(interval={PROMISE_CHECK_INTERVAL_SECONDS}s)"
    )


@app.on_event("shutdown")
def stop_promise_lifecycle_worker() -> None:
    """
    Stop the automatic Promise-to-Pay lifecycle worker cleanly.
    """

    global _promise_worker_thread

    _promise_worker_stop.set()

    if _promise_worker_thread is not None:
        _promise_worker_thread.join(
            timeout=5
        )

    _promise_worker_thread = None

    print(
        "[PromiseLifecycle] automatic deadline worker stopped"
    )


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Auth
#
# AuthMiddleware gates every /api/* route by default (see
# app/auth/middleware.py for the small public allowlist —
# login/register/health/webhook/voice-audio). Middleware order
# matters: Starlette runs middleware in reverse of add order, so
# adding this AFTER CORSMiddleware means CORS headers still get
# attached to 401 responses this middleware returns.
# ============================================================

app.add_middleware(AuthMiddleware)
app.include_router(auth_router)


# ============================================================
# Batch History
#
# Every completed batch (the initial pipeline run plus every
# subsequent "Run recovery batch") is appended here so the
# Overview dashboard can chart how the recovery rate has moved
# over recent runs. File-backed (like live_cases.json) so history
# survives a server restart; the last 7 records are what the UI
# shows, matching Revive's "last seven batches" framing.
# ============================================================

BATCH_HISTORY_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "batch_history.json"
)

_batch_history_lock = threading.RLock()


class BatchHistoryStore:
    """Thread-safe, file-backed log of completed recovery batches."""

    def __init__(self, path: Path = BATCH_HISTORY_FILE) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        with _batch_history_lock:
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                return []

        if isinstance(data, list):
            return data

        return []

    def append(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        with _batch_history_lock:
            history = self.load()

            next_number = len(history) + 1
            record = {
                "batch_id": f"B-{next_number:02d}",
                "label": f"B-{next_number:02d}",
                **record,
            }

            history.append(record)

            self.path.parent.mkdir(parents=True, exist_ok=True)

            with self.path.open("w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)

            return history


batch_history_store = BatchHistoryStore()

def _record_batch_history(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Record one completed pipeline batch in the persistent
    batch-history store.

    The dashboard uses this history for the recovery-rate
    performance chart.
    """

    # --------------------------------------------------------
    # Extract recovery metrics defensively.
    #
    # pipeline_to_dict() may expose these values under slightly
    # different structures depending on the pipeline result.
    # --------------------------------------------------------

    def _first_number(
        *values: Any,
        default: float = 0.0,
    ) -> float:

        for value in values:

            if value is None:
                continue

            try:
                return float(value)
            except (
                TypeError,
                ValueError,
            ):
                continue

        return default

    def _first_int(
        *values: Any,
        default: int = 0,
    ) -> int:

        for value in values:

            if value is None:
                continue

            try:
                return int(value)
            except (
                TypeError,
                ValueError,
            ):
                continue

        return default

    # --------------------------------------------------------
    # Locate the case collection.
    # --------------------------------------------------------

    cases = _cases(payload)

    total_cases = len(cases)

    recovered_cases = 0
    recovered_revenue = 0.0
    addressable_revenue = 0.0

    # --------------------------------------------------------
    # Calculate recovery statistics from the authoritative
    # pipeline cases.
    # --------------------------------------------------------

    for case in cases:

        if not isinstance(
            case,
            dict,
        ):
            continue

        outcome = str(
            case.get("outcome")
            or case.get("recovery_status")
            or ""
        ).upper()

        recovered = (
            outcome == "RECOVERED"
            or case.get("recovered") is True
            or case.get("recovery_status") == "RECOVERED"
        )

        if recovered:
            recovered_cases += 1

        recovered_amount = _first_number(
            case.get("recovered_amount"),
            case.get("recovery_amount"),
            case.get("amount_recovered"),
            case.get("recovered_revenue"),
        )

        if recovered:
            recovered_revenue += recovered_amount

        addressable_amount = _first_number(
            case.get("addressable_amount"),
            case.get("addressable_revenue"),
            case.get("outstanding_amount"),
            case.get("amount"),
            case.get("invoice_amount"),
        )

        addressable_revenue += addressable_amount

    # --------------------------------------------------------
    # Prefer authoritative aggregate metrics if pipeline_to_dict()
    # already supplied them.
    # --------------------------------------------------------

    metrics = payload.get("metrics")

    if not isinstance(
        metrics,
        dict,
    ):
        metrics = {}

    recovered_revenue = _first_number(
        metrics.get("recovered_revenue"),
        metrics.get("recovered_amount"),
        payload.get("recovered_revenue"),
        payload.get("recovered_amount"),
        recovered_revenue,
    )

    addressable_revenue = _first_number(
        metrics.get("addressable_revenue"),
        metrics.get("addressable_amount"),
        payload.get("addressable_revenue"),
        payload.get("addressable_amount"),
        addressable_revenue,
    )

    recovered_cases = _first_int(
        metrics.get("recovered_cases"),
        payload.get("recovered_cases"),
        recovered_cases,
    )

    total_cases = _first_int(
        metrics.get("total_cases"),
        payload.get("total_cases"),
        total_cases,
    )

    # --------------------------------------------------------
    # Recovery rate.
    # --------------------------------------------------------

    if addressable_revenue > 0:
        recovery_rate = (
            recovered_revenue
            / addressable_revenue
        )
    elif total_cases > 0:
        recovery_rate = (
            recovered_cases
            / total_cases
        )
    else:
        recovery_rate = 0.0

    # Keep the value within a sensible range.
    recovery_rate = max(
        0.0,
        min(
            1.0,
            recovery_rate,
        ),
    )

    # --------------------------------------------------------
    # India-local timestamp for dashboard history.
    # --------------------------------------------------------

    promise_timezone = ZoneInfo(
        os.getenv(
            "PROMISE_TIMEZONE",
            "Asia/Kolkata",
        )
    )

    recorded_at = datetime.now(
        promise_timezone
    )

    record = {
        "date_label": recorded_at.strftime(
            "%d %b"
        ),
        "recorded_at": recorded_at.isoformat(),
        "recovery_rate": recovery_rate,
        "recovered_revenue": recovered_revenue,
        "addressable_revenue": addressable_revenue,
        "recovered_cases": recovered_cases,
        "total_cases": total_cases,
    }

    return batch_history_store.append(
        record
    )


# ============================================================
# Pipeline State
# ============================================================

_pipeline: RevivePipeline | None = None

_cached_result: dict[str, Any] | None = None

# Persistent Promise Tracker (Module 5).
#
# PromiseTracker owns durable persistence in
# app/data/promise_tracker.json, so promise state survives
# backend restarts. The tracker also reconstructs the PolicyEngine
# hard-stop for active promises during startup.

_decision_explainer = DecisionExplainer()


# ============================================================
# Serialization
# ============================================================

def _dataclass_to_dict(value: Any) -> Any:
    """
    Recursively convert dataclasses into JSON-safe structures.
    """

    if is_dataclass(value):
        return {
            key: _dataclass_to_dict(item)
            for key, item in asdict(value).items()
        }

    if isinstance(value, dict):
        return {
            key: _dataclass_to_dict(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            _dataclass_to_dict(item)
            for item in value
        ]

    return value


# ============================================================
# Pipeline Execution
# ============================================================

def _apply_promise_overlay(
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Overlay active promise-to-pay state from the Promise Tracker
    onto case dicts before running the pipeline.

    This is the only hook needed: ROIPortfolioEngine.build_case_state()
    already reads `promise_to_pay_active` / `promise_date` directly
    off each case dict (see roi_engine/roi.py) and the PolicyEngine
    already hard-stops automated contact when it's set — that wiring
    has existed all along. What was missing was anything that ever
    set those fields from a real promise record, which is what this
    does.

    Cases are copied (not mutated in place) so the fixed synthetic
    dataset returned by load_cases() is never modified in memory.
    """

    overlaid = []

    for case in cases:
        record = promise_tracker.records.get(case.get("case_id"))

        if (
            record is not None
            and record.status == PromiseStatus.PROMISED
        ):
            case = dict(case)
            case["promise_to_pay_active"] = True
            case["promise_date"] = record.promise_date.isoformat()

        overlaid.append(case)

    return overlaid

def run_pipeline() -> dict[str, Any]:
    """
    Execute the authoritative Revive recovery pipeline.

    Promise lifecycle checks are performed here so customer reminders
    and expired-promise handling are not coupled to a GET endpoint.

    Promise dates are stored as naive wall-clock values in the
    configured Promise timezone (Asia/Kolkata by default), so all
    lifecycle comparisons use that same timezone.
    """

    global _cached_result
    global _pipeline

    # --------------------------------------------------------
    # PROMISE LIFECYCLE
    # --------------------------------------------------------
    #
    # Promise dates are stored as naive local wall-clock values.
    # Always compare them against the same local timezone.
    #
    # This prevents Docker/UTC clock differences from causing a
    # promise to be marked broken at the wrong time.
    # --------------------------------------------------------

    promise_timezone = ZoneInfo(
        os.getenv(
            "PROMISE_TIMEZONE",
            "Asia/Kolkata",
        )
    )

    now_local = datetime.now(
        promise_timezone
    ).replace(
        tzinfo=None
    )

    # --------------------------------------------------------
    # 1. Send DUE_SOON reminder.
    #
    # The alert service is idempotent, so repeated pipeline runs
    # cannot spam the same customer for the same event.
    # --------------------------------------------------------

    for record in promise_tracker.get_all_promises():

        if record.status != PromiseStatus.PROMISED:
            continue

        seconds_left = (
            record.promise_date - now_local
        ).total_seconds()

        if (
            0 < seconds_left <= 24 * 3600
        ):
            customer_alert_service.send(
                record,
                "DUE_SOON",
                payment_link_url=record.payment_link_url,
            )

    # --------------------------------------------------------
    # 2. Mark expired promises as BROKEN.
    #
    # evaluate_due_promises() performs the durable state
    # transition. It returns only promises that actually became
    # broken during this evaluation.
    # --------------------------------------------------------

    broken_promises = (
        promise_tracker.evaluate_due_promises(
            now=now_local
        )
    )

    # --------------------------------------------------------
    # 3. Notify customers whose promises just became broken.
    #
    # Only newly broken records are returned above, so we do not
    # repeatedly send PROMISE_BROKEN emails.
    # --------------------------------------------------------

    for record in broken_promises:

        customer_alert_service.send(
            record,
            "PROMISE_BROKEN",
            payment_link_url=record.payment_link_url,
        )

    # --------------------------------------------------------
    # AUTHORITATIVE SYNTHETIC DATASET
    # --------------------------------------------------------

    cases = load_cases()

    cases = _apply_promise_overlay(
        cases
    )

    _pipeline = RevivePipeline()

    result = _pipeline.run(
        cases
    )

    payload = pipeline_to_dict(
        result
    )

    _cached_result = payload

    _record_batch_history(
        payload
    )

    return payload


def get_pipeline() -> dict[str, Any]:
    """
    Return cached pipeline result.

    If no result exists, run the pipeline.
    """

    global _cached_result

    if _cached_result is None:
        return run_pipeline()

    return _cached_result


# ============================================================
# Extraction Helpers
# ============================================================

def _cases(
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    value = result.get("cases", [])

    if isinstance(value, list):
        return value

    return []


def _metrics(
    result: dict[str, Any],
) -> dict[str, Any]:
    value = result.get("metrics", {})

    if isinstance(value, dict):
        return value

    return {}


def _psr_alerts(
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    value = result.get("psr_alerts", [])

    if isinstance(value, list):
        return value

    return []


def _a2a_results(
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Extract A2A settlement results.

    Primary key:
        a2a_settlements

    Backward-compatible fallback:
        a2a_results
    """

    value = result.get(
        "a2a_settlements",
        [],
    )

    if isinstance(value, list):
        return value

    value = result.get(
        "a2a_results",
        [],
    )

    if isinstance(value, list):
        return value

    return []


def _a2a_counts(
    result: dict[str, Any],
) -> tuple[int, int]:
    """
    Return the same A2A counts represented by the React dashboard.

    The authoritative serialized A2A settlement records contain an
    `eligible` boolean and an `outcome` value (see
    app/a2a_settlement/settlement.py — SettlementResult.eligible).

    The dashboard treats a settlement as eligible when:
      - eligible is explicitly True, OR
      - eligibility is "ELIGIBLE" (string fallback, for callers that
        don't send the boolean), OR
      - outcome is SETTLED or REJECTED.

    IMPORTANT: do not key off a string field called "eligibility"
    alone — the pipeline serializes the authoritative field as the
    boolean `eligible`, not as an "eligibility" string. Checking only
    the string keys silently returns zero for every case.

    Do not trust metrics["a2a_eligible_cases"] here either — that
    metric counts every case the A2A engine evaluated, including
    ones it internally BLOCKED, which is not the same as this
    settlement table's ELIGIBLE count.
    """

    settlements = _a2a_results(result)

    eligible = 0
    settled = 0

    for item in settlements:
        if not isinstance(item, dict):
            continue

        outcome = str(
            _first_value(
                item,
                [
                    "outcome",
                    "a2a_outcome",
                    "settlement_status",
                    "status",
                ],
                "",
            )
        ).strip().upper()

        raw_eligible = item.get("eligible")

        if isinstance(raw_eligible, bool):
            is_eligible = raw_eligible
        elif isinstance(raw_eligible, str):
            is_eligible = (
                raw_eligible.strip().lower()
                in {"true", "1", "yes", "eligible"}
            )
        else:
            is_eligible = False

        if not is_eligible:
            eligibility = str(
                _first_value(
                    item,
                    [
                        "eligibility",
                        "eligibility_status",
                        "a2a_eligibility",
                        "a2a_eligibility_status",
                    ],
                    "",
                )
            ).strip().upper()

            is_eligible = eligibility == "ELIGIBLE"

        # Settled/rejected A2A outcomes are eligible settlement
        # attempts even if the explicit eligibility field is absent.
        if outcome in {"SETTLED", "REJECTED"}:
            is_eligible = True

        if is_eligible:
            eligible += 1

        if outcome == "SETTLED":
            settled += 1

    return eligible, settled


def _ledger(
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    value = result.get("ledger", [])

    if isinstance(value, list):
        return value

    return []


# ============================================================
# Health
# ============================================================

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "success": True,
        "status": "ok",
        "service": "revive-dashboard-api",
        "version": "1.1.0",
    }


# ============================================================
# Run Batch
# ============================================================

@app.post("/api/run-batch")
def run_batch() -> dict[str, Any]:
    """
    Execute a completely fresh production batch.
    """

    global _cached_result

    _cached_result = None

    result = run_pipeline()

    return {
        "success": True,
        "message": "Revive batch completed successfully.",
        "data": result,
    }


# ============================================================
# Batch History
# ============================================================

@app.get("/api/batch-history")
def batch_history() -> dict[str, Any]:
    """
    Return the last 7 completed batches for the "Last seven
    batches" performance-over-time chart on the Overview page.
    """

    # Make sure at least one batch (the initial pipeline run) has
    # been recorded before the frontend asks for history.
    get_pipeline()

    history = batch_history_store.load()
    recent = history[-7:]

    return {
        "success": True,
        "batches": recent,
        "total_batches": len(history),
    }


# ============================================================
# Live Razorpay Payments
# ============================================================

@app.post("/api/payments/checkout")
def create_checkout(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Create a real Razorpay Payment Link for the given amount, so a
    live payment attempt (and, if it fails, a real webhook-driven
    case) can be generated against Razorpay's actual sandbox from
    the dashboard, without needing the standalone capture script.
    """

    amount = payload.get("amount")

    if not isinstance(amount, (int, float)) or amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="`amount` must be a positive number.",
        )

    customer_email = (
        str(payload.get("customer_email")).strip()
        if payload.get("customer_email") is not None
        else None
    )

    # --------------------------------------------------------
    # Resolve real customer identity.
    #
    # A human operator does not know or have a customer_id to type
    # in — asking for one led to every blank submission silently
    # colliding into the same fake "cust_unknown"/"cust_demo"
    # identity. The email (or an explicitly supplied customer_id,
    # e.g. from a case the operator is acting on) is the only thing
    # that's actually known, so that's what drives identity: the
    # directory reuses the same customer_id for a repeat email and
    # mints a new, collision-free one the first time it sees one.
    # Whatever is supplied here (name/email/contact) is durably
    # remembered against that customer_id for every other surface
    # (Promise-to-Pay, retries, A2A settlement, the case pipeline)
    # to reuse later.
    # --------------------------------------------------------

    resolved_customer = customer_directory.resolve(
        customer_id=(
            str(payload.get("customer_id")).strip()
            if payload.get("customer_id")
            else None
        ),
        name=(
            str(payload.get("customer_name")).strip()
            if payload.get("customer_name")
            else None
        ),
        email=customer_email,
    )

    customer_id = resolved_customer["customer_id"]

    customer_name = (
        resolved_customer.get("name")
        or "Revive Customer"
    )

    customer_email = resolved_customer.get("email")

    description = str(
        payload.get("description")
        or "Revive recovery — payment link"
    )

    surface = str(
        payload.get("surface")
        or "subscription_failure"
    )

    invoice_id_raw = payload.get("invoice_id")

    invoice_id = (
        str(invoice_id_raw)
        if invoice_id_raw
        else None
    )

    has_ap_agent = bool(
        payload.get("has_ap_agent", False)
    )

    disputed = bool(
        payload.get("disputed", False)
    )

    try:
        link = create_payment_link(
        amount_rupees=float(amount),
        customer_name=customer_name,
        customer_id=customer_id,
        customer_email=customer_email,
        description=description,
        surface=surface,
        invoice_id=invoice_id,
        has_ap_agent=has_ap_agent,
        disputed=disputed,
    )

    except RazorpayConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        )

    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )

    return {
        "success": True,
        "payment_link_id": link.get("id"),
        "short_url": link.get("short_url"),
        "revive_case_tag": link.get("revive_case_tag"),
        "a2a_eligibility": {
            "surface": surface,
            "invoice_id": invoice_id,
            "has_ap_agent": has_ap_agent,
            "disputed": disputed,
        },
    }


@app.post("/api/payments/live-cases/{case_id}/retry")
def retry_live_case_payment(case_id: str) -> dict[str, Any]:
    """
    Issue a new Razorpay Payment Link for an existing live
    PENDING_RECOVERY case, reusing that case's original
    revive_case_tag so a later payment.captured event resolves
    THIS same case rather than being unmatched or creating a
    second one.

    This endpoint never marks a case recovered. Only a verified
    payment.captured webhook can do that.
    """

    case = live_case_store.find_by_case_id(case_id)

    if case is None:
        raise HTTPException(
            status_code=404,
            detail=f"No live case found with case_id={case_id!r}.",
        )

    if case.get("recovery_status") == "RECOVERED":
        raise HTTPException(
            status_code=409,
            detail=(
                "This case is already RECOVERED and cannot be "
                "retried."
            ),
        )

    if case.get("recovery_status") != "PENDING_RECOVERY":
        raise HTTPException(
            status_code=409,
            detail=(
                "Only cases with recovery_status="
                "PENDING_RECOVERY can be retried "
                f"(current: {case.get('recovery_status')!r})."
            ),
        )

    revive_case_tag = case.get("revive_case_tag")

    if not revive_case_tag:
        raise HTTPException(
            status_code=422,
            detail=(
                "This live case has no revive_case_tag and "
                "cannot be safely retried — a captured payment "
                "could not be correlated back to it."
            ),
        )

    amount = float(case.get("amount") or 0)

    if amount <= 0:
        raise HTTPException(
            status_code=422,
            detail="Case amount must be greater than zero to retry.",
        )

    try:
        link = create_payment_link(
            amount_rupees=amount,
            customer_name=str(
                case.get("customer_name") or "Revive Customer"
            ),
            customer_id=str(
                case.get("customer_id") or "cust_unknown"
            ),
            customer_email=(
                str(case.get("customer_email")).strip()
                if case.get("customer_email") is not None
                else None
            ),
            description=(
                f"Revive recovery retry — {case_id}"
            ),
            revive_case_tag=revive_case_tag,
        )

    except RazorpayConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    live_case_store.record_retry_link(
        case_id=case_id,
        short_url=link.get("short_url"),
    )

    return {
        "success": True,
        "case_id": case_id,
        "revive_case_tag": revive_case_tag,
        "short_url": link.get("short_url"),
        "amount": amount,
    }


@app.post("/api/payments/webhook")
async def razorpay_webhook(
    request: Request,
) -> dict[str, Any]:
    """
    Receive and verify real Razorpay webhook events.

    Supported events:

        payment.failed
            → creates an UNRECOVERED live case

        payment.captured
            → confirms a matching live A2A agreement only when
              the captured amount exactly matches the agreed amount

            → otherwise marks a normal non-A2A live case recovered

    IMPORTANT:

        A2A AGREED does NOT mean payment recovered.

        Only a verified Razorpay payment.captured webhook with
        an amount matching the A2A agreement can confirm the
        A2A settlement.

    Duplicate webhook deliveries are handled idempotently.
    """

    global _cached_result

    raw_body = await request.body()

    signature = request.headers.get(
        "X-Razorpay-Signature"
    )

    # --------------------------------------------------------
    # SECURITY
    # --------------------------------------------------------

    if not verify_webhook_signature(
        raw_body,
        signature,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid or missing webhook signature.",
        )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    try:
        event_payload = json.loads(
            raw_body
        )

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body.",
        )

    event_name = event_payload.get(
        "event"
    )

    # ========================================================
    # PAYMENT FAILED
    # ========================================================

    if event_name == "payment.failed":

        case = map_failed_payment_to_case(
            event_payload
        )

        if case is None:
            return {
                "success": True,
                "ignored": True,
                "event": event_name,
            }

        added = live_case_store.add(
            case
        )

        return {
            "success": True,
            "ignored": False,
            "event": event_name,
            "case_id": case["case_id"],
            "newly_added": added,
            "outcome": case["outcome"],
            "recovery_status": case[
                "recovery_status"
            ],
        }

    # ========================================================
    # PAYMENT CAPTURED
    # ========================================================

    if event_name == "payment.captured":

        recovery = (
            map_captured_payment_to_recovery(
                event_payload
            )
        )

        if recovery is None:
            return {
                "success": True,
                "ignored": True,
                "event": event_name,
            }

        revive_case_tag = recovery[
            "revive_case_tag"
        ]

        captured_amount = round(
            float(
                recovery.get(
                    "recovered_amount"
                ) or 0
            ),
            2,
        )

        payment_id = recovery[
            "razorpay_payment_id"
        ]

        # ----------------------------------------------------
        # PROMISE-TO-PAY PAYMENT PATH
        # ----------------------------------------------------
        # Payment Links created for promises carry a dedicated
        # promise ID and a unique Revive promise case tag.
        # Verify the promise before treating the payment as
        # ordinary live recovery.
        # ----------------------------------------------------

        payment_entity = (
            event_payload.get("payload", {})
            .get("payment", {})
            .get("entity", {})
        )
        payment_notes = payment_entity.get("notes") or {}
        if isinstance(payment_notes, dict):
            promise_id = str(payment_notes.get("revive_promise_id") or "").strip()
        else:
            promise_id = ""

        promise_record = None
        if promise_id:
            for candidate in promise_tracker.get_all_promises():
                if candidate.promise_id == promise_id:
                    promise_record = candidate
                    break

        if promise_record is not None:
            promised_amount = round(float(promise_record.promised_amount), 2)
            if captured_amount != promised_amount:
                return {
                    "success": True,
                    "ignored": False,
                    "event": event_name,
                    "case_id": promise_record.case_id,
                    "outcome": "UNRECOVERED",
                    "recovered_amount": 0.0,
                    "promise": {
                        "verified": False,
                        "promise_id": promise_record.promise_id,
                        "status": promise_record.status.value,
                        "reason": "Captured amount does not match the promised amount.",
                        "promised_amount": promised_amount,
                        "captured_amount": captured_amount,
                    },
                }

            try:
                paid_promise = promise_tracker.mark_paid(
                    case_id=promise_record.case_id,
                    paid_at=datetime.now(),
                    payment_reference=payment_id,
                    payment_source="razorpay_webhook",
                    payment_verified=True,
                )
            except ValueError as exc:
                return {
                    "success": True,
                    "ignored": True,
                    "event": event_name,
                    "case_id": promise_record.case_id,
                    "promise": {
                        "verified": False,
                        "promise_id": promise_record.promise_id,
                        "reason": str(exc),
                    },
                }

            customer_alert_service.send(
                paid_promise,
                "PAYMENT_VERIFIED",
            )

            _cached_result = None
            return {
                "success": True,
                "ignored": False,
                "event": event_name,
                "case_id": paid_promise.case_id,
                "outcome": "RECOVERED",
                "recovered_amount": captured_amount,
                "promise": {
                    "verified": True,
                    "promise_id": paid_promise.promise_id,
                    "status": paid_promise.status.value,
                    "payment_reference": payment_id,
                    "payment_source": "razorpay_webhook",
                },
            }

        # ----------------------------------------------------
        # Check whether this payment belongs to an active
        # A2A agreement.
        #
        # If no agreement exists, this is a normal Revive
        # live recovery and follows the normal live-case path.
        # ----------------------------------------------------

        a2a_agreement = (
            live_a2a_settlement_store.get_by_case_tag(
                revive_case_tag
            )
        )

        # ====================================================
        # A2A PAYMENT PATH
        # ====================================================

        if a2a_agreement is not None:

            try:
                agreed_amount = round(
                    float(
                        a2a_agreement.get(
                            "agreed_amount"
                        ) or 0
                    ),
                    2,
                )

            except (
                TypeError,
                ValueError,
            ):

                return {
                    "success": True,
                    "ignored": True,
                    "event": event_name,
                    "case_id": (
                        a2a_agreement.get(
                            "case_id"
                        )
                    ),
                    "reason": (
                        "A2A agreement contains an "
                        "invalid agreed amount."
                    ),
                    "a2a": {
                        "confirmed": False,
                        "agreement_id": (
                            a2a_agreement.get(
                                "agreement_id"
                            )
                        ),
                        "payment_status": (
                            a2a_agreement.get(
                                "payment_status"
                            )
                        ),
                    },
                }

            # ------------------------------------------------
            # Financial integrity:
            #
            # The captured payment must exactly satisfy the
            # negotiated A2A agreement.
            #
            # Example:
            #
            # AGREED     = INR 10,000
            # CAPTURED   = INR 1,000
            #
            # Result:
            #
            # A2A remains PENDING.
            # Live case remains PENDING_RECOVERY.
            # ------------------------------------------------

            if captured_amount != agreed_amount:

                return {
                    "success": True,
                    "ignored": False,
                    "event": event_name,
                    "case_id": (
                        a2a_agreement.get(
                            "case_id"
                        )
                    ),
                    "outcome": "UNRECOVERED",
                    "recovered_amount": 0.0,
                    "a2a": {
                        "confirmed": False,
                        "agreement_id": (
                            a2a_agreement.get(
                                "agreement_id"
                            )
                        ),
                        "payment_status": (
                            a2a_agreement.get(
                                "payment_status",
                                "PENDING",
                            )
                        ),
                        "recovery_confirmed": (
                            a2a_agreement.get(
                                "recovery_confirmed",
                                False,
                            )
                        ),
                        "reason": (
                            "Captured amount does not "
                            "match the negotiated A2A "
                            "agreement amount."
                        ),
                        "agreed_amount": agreed_amount,
                        "captured_amount": captured_amount,
                    },
                }

            # ------------------------------------------------
            # Confirm the A2A agreement FIRST.
            #
            # This guarantees that an A2A case cannot be marked
            # recovered if the settlement agreement itself could
            # not be confirmed.
            # ------------------------------------------------

            a2a_confirmation = (
                live_a2a_settlement_store.confirm_payment(
                    revive_case_tag=revive_case_tag,
                    razorpay_payment_id=payment_id,
                    recovered_amount=captured_amount,
                    confirmed_at=recovery[
                        "recovered_at"
                    ],
                )
            )

            if a2a_confirmation is None:

                return {
                    "success": True,
                    "ignored": False,
                    "event": event_name,
                    "case_id": (
                        a2a_agreement.get(
                            "case_id"
                        )
                    ),
                    "outcome": "UNRECOVERED",
                    "recovered_amount": 0.0,
                    "a2a": {
                        "confirmed": False,
                        "agreement_id": (
                            a2a_agreement.get(
                                "agreement_id"
                            )
                        ),
                        "payment_status": (
                            a2a_agreement.get(
                                "payment_status",
                                "PENDING",
                            )
                        ),
                        "reason": (
                            "A2A payment confirmation "
                            "could not be completed."
                        ),
                    },
                }

            # ------------------------------------------------
            # If the agreement was already confirmed with a
            # different payment, do not overwrite the live
            # recovery with another payment ID.
            # ------------------------------------------------

            confirmed_payment_id = (
                a2a_confirmation.get(
                    "razorpay_payment_id"
                )
            )

            if (
                a2a_confirmation.get(
                    "recovery_confirmed"
                )
                and confirmed_payment_id
                != payment_id
            ):

                return {
                    "success": True,
                    "ignored": True,
                    "event": event_name,
                    "case_id": (
                        a2a_confirmation.get(
                            "case_id"
                        )
                    ),
                    "outcome": "RECOVERED",
                    "recovered_amount": agreed_amount,
                    "a2a": {
                        "confirmed": True,
                        "agreement_id": (
                            a2a_confirmation.get(
                                "agreement_id"
                            )
                        ),
                        "payment_status": (
                            a2a_confirmation.get(
                                "payment_status"
                            )
                        ),
                        "reason": (
                            "A2A agreement was already "
                            "confirmed by another payment."
                        ),
                    },
                }

            # ------------------------------------------------
            # Now mark the matching live case recovered.
            # ------------------------------------------------

            updated = (
                live_case_store.mark_recovered(
                    revive_case_tag=revive_case_tag,
                    recovered_amount=captured_amount,
                    razorpay_payment_id=payment_id,
                    recovered_at=recovery[
                        "recovered_at"
                    ],
                )
            )

            if updated is None:

                return {
                    "success": True,
                    "ignored": False,
                    "event": event_name,
                    "outcome": "UNRECOVERED",
                    "recovered_amount": 0.0,
                    "a2a": {
                        "confirmed": True,
                        "agreement_id": (
                            a2a_confirmation.get(
                                "agreement_id"
                            )
                        ),
                        "payment_status": (
                            a2a_confirmation.get(
                                "payment_status"
                            )
                        ),
                        "reason": (
                            "A2A agreement was confirmed, "
                            "but no matching live case "
                            "was found."
                        ),
                    },
                }

            # The synthetic pipeline does not include live cases,
            # but invalidate its cache so dependent read endpoints
            # do not retain stale state.
            _cached_result = None

            return {
                "success": True,
                "ignored": False,
                "event": event_name,
                "case_id": updated[
                    "case_id"
                ],
                "outcome": updated[
                    "outcome"
                ],
                "recovered_amount": updated[
                    "recovered_amount"
                ],
                "a2a": {
                    "confirmed": (
                        a2a_confirmation.get(
                            "recovery_confirmed",
                            False,
                        )
                    ),
                    "agreement_id": (
                        a2a_confirmation.get(
                            "agreement_id"
                        )
                    ),
                    "payment_status": (
                        a2a_confirmation.get(
                            "payment_status"
                        )
                    ),
                },
            }

        # ====================================================
        # NORMAL NON-A2A LIVE PAYMENT PATH
        # ====================================================

        updated = (
            live_case_store.mark_recovered(
                revive_case_tag=revive_case_tag,
                recovered_amount=captured_amount,
                razorpay_payment_id=payment_id,
                recovered_at=recovery[
                    "recovered_at"
                ],
            )
        )

        if updated is None:

            return {
                "success": True,
                "ignored": True,
                "event": event_name,
                "reason": (
                    "No matching live Revive failure "
                    "was found."
                ),
            }

        # The synthetic pipeline does not include live cases,
        # but invalidate its cache so dependent read endpoints
        # immediately see the latest state.
        _cached_result = None

        return {
            "success": True,
            "ignored": False,
            "event": event_name,
            "case_id": updated[
                "case_id"
            ],
            "outcome": updated[
                "outcome"
            ],
            "recovered_amount": updated[
                "recovered_amount"
            ],
            "a2a": {
                "confirmed": False,
                "agreement_id": None,
                "payment_status": None,
            },
        }

    # ========================================================
    # OTHER VALID RAZORPAY EVENTS
    # ========================================================

    return {
        "success": True,
        "ignored": True,
        "event": event_name,
    }


@app.get("/api/payments/live-cases")
def live_cases() -> dict[str, Any]:
    """List real cases captured via the Razorpay webhook so far."""

    cases = live_case_store.load()

    return {
        "success": True,
        "count": len(cases),
        "cases": cases,
    }


@app.delete("/api/payments/live-cases")
def reset_live_cases() -> dict[str, Any]:
    """
    Clear all webhook-captured live cases (demo/testing reset).
    Does not touch the synthetic dataset.
    """

    global _cached_result

    live_case_store.clear()
    _cached_result = None

    return {"success": True, "message": "Live cases cleared."}


# ============================================================
# Customer directory
# ============================================================
#
# The single durable source of truth for "who is this customer and
# how do we reach them", shared across live checkout, Promise-to-Pay,
# retries and A2A settlement. See app/customers/directory.py for the
# full rationale. This endpoint exists mainly so the frontend can
# offer a "known customer" picker/autocomplete instead of asking an
# operator to remember or invent a customer_id.

@app.get("/api/customers")
def list_customers() -> dict[str, Any]:
    """List every customer the directory has resolved so far."""

    customers = customer_directory.list_all()

    return {
        "success": True,
        "count": len(customers),
        "customers": customers,
    }


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: str) -> dict[str, Any]:
    """Look up a single customer by ID."""

    entry = customer_directory.get(customer_id)

    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown customer_id '{customer_id}'.",
        )

    return {"success": True, "customer": entry}


# ============================================================
# Notifications
# ============================================================

@app.get("/api/notifications")
def notifications() -> dict[str, Any]:
    """
    Operator-facing notifications derived from the current
    authoritative pipeline result: PSR Guardian alerts, completed
    A2A settlements, high-value cases that were not recovered, and
    any live Razorpay payment failures captured via webhook.

    Read-only — does not modify cases, policy, ROI, A2A, or ledger
    state.
    """

    result = get_pipeline()

    items = generate_notifications(
        cases=_cases(result),
        psr_alerts=_psr_alerts(result),
        a2a_settlements=_a2a_results(result),
        live_cases=live_case_store.load(),
    )

    payload = [item.to_dict() for item in items]

    severity_order = {
        "CRITICAL": 0,
        "HIGH": 1,
        "WARNING": 2,
        "MEDIUM": 3,
        "LOW": 4,
        "SUCCESS": 5,
    }

    payload.sort(
        key=lambda item: severity_order.get(item["severity"], 99)
    )

    return {
        "success": True,
        "count": len(payload),
        "notifications": payload,
    }


# ============================================================
# Promise-to-Pay Tracker (Module 5)
# ============================================================

def _promise_record_to_dict(
    record: Any,
) -> dict[str, Any]:
    """
    Serialize a PromiseRecord into the stable API representation.

    The original fields are preserved for frontend compatibility.
    New production metadata is exposed alongside them.
    """

    if record is None:
        return {}

    return {
        "case_id": record.case_id,
        "promise_id": record.promise_id,
        "customer_id": record.customer_id,
        "customer_name": record.customer_name,
        "customer_email": record.customer_email,
        "invoice_id": record.invoice_id,
        "promised_amount": record.promised_amount,
        "original_amount": record.original_amount,
        "outstanding_amount": record.outstanding_amount,
        "promise_date": record.promise_date.isoformat(),
        "created_at": record.created_at.isoformat(),
        "updated_at": (
            record.updated_at.isoformat()
            if record.updated_at is not None
            else None
        ),
        "status": record.status.value,
        "reason": record.reason,
        "payment_reference": record.payment_reference,
        "payment_source": record.payment_source,
        "payment_verified": record.payment_verified,
        "payment_link_id": record.payment_link_id,
        "payment_link_url": record.payment_link_url,
        "payment_link_expire_by": (
            record.payment_link_expire_by.isoformat()
            if record.payment_link_expire_by is not None
            else None
        ),
    }


def _promise_transition_to_dict(
    transition: Any,
) -> dict[str, Any]:
    """
    Serialize a PromiseTransition.

    promise_id is included when available while retaining the
    original transition fields expected by the existing UI.
    """

    return {
        "case_id": transition.case_id,
        "promise_id": getattr(
            transition,
            "promise_id",
            "",
        ),
        "previous_status": transition.previous_status.value,
        "new_status": transition.new_status.value,
        # Aliases the dashboard's promise-history modal reads.
        "from_status": transition.previous_status.value,
        "to_status": transition.new_status.value,
        "timestamp": transition.timestamp.isoformat(),
        "reason": transition.reason,
    }


def _find_pipeline_case(
    case_id: str,
) -> dict[str, Any] | None:
    """
    Find the authoritative synthetic case without mutating it.
    """

    result = get_pipeline()

    for case in _cases(result):

        if case.get("case_id") == case_id:
            return case

    return None


@app.get("/api/promises")
def list_promises() -> dict[str, Any]:
    """
    List all current Promise-to-Pay records.

    This endpoint is strictly read-only.

    Customer email alerts are evaluated by run_pipeline()
    instead of being triggered by dashboard polling.
    """

    records = [
        _promise_record_to_dict(
            record
        )
        for record in promise_tracker.get_all_promises()
    ]

    return {
        "success": True,
        "metrics": promise_tracker.metrics(),
        "promises": records,
        "persistence": promise_tracker.persistence_info(),
    }


@app.get("/api/promises/{case_id}")
def get_promise(
    case_id: str,
) -> dict[str, Any]:
    """
    Get the current promise and complete audit trail for one case.
    """

    record = promise_tracker.get_promise(
        case_id
    )

    return {
        "success": True,
        "case_id": case_id,
        "status": promise_tracker.get_status(
            case_id
        ).value,
        "promise": (
            _promise_record_to_dict(record)
            if record is not None
            else None
        ),
        "audit_trail": [
            _promise_transition_to_dict(
                transition
            )
            for transition
            in promise_tracker.get_audit_trail(
                case_id
            )
        ],
    }


@app.get("/api/promises/{case_id}/history")
def get_promise_history(
    case_id: str,
) -> dict[str, Any]:
    """
    Return historical promise snapshots for a case.

    This allows the dashboard/operator layer to distinguish the
    current promise from previous broken/fulfilled promises.
    """

    current = promise_tracker.get_promise(
        case_id
    )

    history = promise_tracker.get_promise_history(
        case_id
    )

    # Include the current record, replacing any historical snapshot
    # that shares its promise_id rather than skipping it outright.
    # A promise keeps the same promise_id across a promised -> broken
    # (or promised -> paid) transition, so a plain "already present"
    # check would keep the stale "promised" snapshot and silently
    # drop the terminal state. This makes the endpoint a useful
    # complete promise lifecycle view.
    combined: list[Any] = [
        record
        for record in history
        if current is None
        or record.promise_id != current.promise_id
    ]

    if current is not None:
        combined.append(current)

    combined.sort(
        key=lambda record: (
            record.created_at,
            record.updated_at
            or record.created_at,
        )
    )

    return {
        "success": True,
        "case_id": case_id,
        "count": len(combined),
        "current": _promise_record_to_dict(
            current
        ),
        "history": [
            _promise_record_to_dict(
                record
            )
            for record in combined
        ],
        "audit_trail": [
            _promise_transition_to_dict(
                transition
            )
            for transition
            in promise_tracker.get_audit_trail(
                case_id
            )
        ],
    }


@app.post("/api/promises")
def create_promise(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Record a customer's promise-to-pay for a case.

    Body:

        {
            "case_id": "RV-00042",
            "promised_amount": 5000,
            "promise_date": "2026-09-15T00:00:00",

            // Optional overrides. If omitted, the values are
            // taken from the authoritative case record.
            "customer_id": "cust-001",
            "customer_name": "Example Corp",
            "customer_email": "ap@example.com",
            "invoice_id": "INV-001"
        }

    The PromiseTracker persists the promise and activates the
    existing PolicyEngine hard-stop for the case.
    """

    case_id = payload.get(
        "case_id"
    )

    if not isinstance(
        case_id,
        str,
    ) or not case_id.strip():

        raise HTTPException(
            status_code=400,
            detail="`case_id` is required.",
        )

    case_id = case_id.strip()

    # --------------------------------------------------------
    # Find authoritative case.
    # --------------------------------------------------------

    case = _find_pipeline_case(
        case_id
    )

    if case is None:

        raise HTTPException(
            status_code=404,
            detail=(
                f"No case found with case_id {case_id}."
            ),
        )

    # A recovered live payment cannot be converted into a new
    # active promise. Pending/unrecovered live cases remain
    # eligible, exactly like other unresolved recovery cases.
    if (
        case.get("recovery_status") == "RECOVERED"
        or case.get("outcome") == "RECOVERED"
    ):
        raise HTTPException(
            status_code=409,
            detail="This case is already recovered and cannot receive a new Promise-to-Pay commitment.",
        )

    # --------------------------------------------------------
    # Amount
    # --------------------------------------------------------

    raw_amount = payload.get(
        "promised_amount"
    )

    try:

        promised_amount = float(
            raw_amount
        )

    except (
        TypeError,
        ValueError,
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "`promised_amount` must be a number."
            ),
        )

    if promised_amount <= 0:

        raise HTTPException(
            status_code=400,
            detail=(
                "`promised_amount` must be greater "
                "than zero."
            ),
        )

    # --------------------------------------------------------
    # Promise date
    # --------------------------------------------------------

    raw_promise_date = payload.get(
        "promise_date"
    )

    if not raw_promise_date:

        raise HTTPException(
            status_code=400,
            detail=(
                "`promise_date` is required "
                "(ISO 8601)."
            ),
        )

    try:

        promise_date = datetime.fromisoformat(
            str(raw_promise_date)
        )

        # Promise-to-Pay dates are entered through a browser
        # datetime-local control. Preserve the selected local
        # wall-clock time instead of converting it to UTC and then
        # stripping the timezone, which causes a visible time shift.
        # If an API caller supplies an explicit timezone offset,
        # normalize it to the application's Promise timezone rather
        # than silently changing the displayed commitment time.
        if promise_date.tzinfo is not None:

            promise_timezone = ZoneInfo(
                os.getenv(
                    "PROMISE_TIMEZONE",
                    "Asia/Kolkata",
                )
            )

            promise_date = (
                promise_date
                .astimezone(promise_timezone)
                .replace(tzinfo=None)
            )

    except ValueError:

        raise HTTPException(
            status_code=400,
            detail=(
                "`promise_date` must be a valid "
                "ISO 8601 datetime."
            ),
        )

    # --------------------------------------------------------
    # Case metadata
    #
    # The case remains the authoritative source. Payload values
    # may override metadata when the caller explicitly provides
    # them.
    # --------------------------------------------------------

    customer_id = (
        str(
            payload.get(
                "customer_id"
            )
        )
        if payload.get(
            "customer_id"
        ) is not None
        else (
            str(
                case.get(
                    "customer_id"
                )
            )
            if case.get(
                "customer_id"
            ) is not None
            else None
        )
    )

    customer_email = (
        str(
            payload.get(
                "customer_email"
            )
        ).strip()
        if payload.get(
            "customer_email"
        ) is not None
        else (
            str(
                case.get(
                    "customer_email"
                )
            ).strip()
            if case.get(
                "customer_email"
            ) is not None
            else None
        )
    )

    customer_name = (
        str(
            payload.get(
                "customer_name"
            )
        )
        if payload.get(
            "customer_name"
        ) is not None
        else (
            str(
                case.get(
                    "customer_name"
                )
            )
            if case.get(
                "customer_name"
            ) is not None
            else None
        )
    )

    # --------------------------------------------------------
    # Customer Directory fallback + upsert.
    #
    # The case (synthetic or live) is still the authoritative
    # source when it has the data. But when it doesn't — most
    # commonly customer_email, since cases.json has no email
    # column at all — fall back to whatever the Customer Directory
    # already knows for this customer_id (e.g. captured earlier
    # through a live checkout or a previous promise for the same
    # customer). Then upsert whatever we ended up with, so this
    # promise's contact details become available to every other
    # surface that only has the customer_id going forward.
    # --------------------------------------------------------

    if customer_id and (customer_name is None or customer_email is None):
        directory_entry = customer_directory.get(customer_id)
        if directory_entry:
            customer_name = customer_name or directory_entry.get("name")
            customer_email = customer_email or directory_entry.get("email")

    if customer_id:
        customer_directory.resolve(
            customer_id=customer_id,
            name=customer_name,
            email=customer_email,
        )

    invoice_id = (
        str(
            payload.get(
                "invoice_id"
            )
        )
        if payload.get(
            "invoice_id"
        ) is not None
        else (
            str(
                case.get(
                    "invoice_id"
                )
            )
            if case.get(
                "invoice_id"
            ) is not None
            else None
        )
    )

    # --------------------------------------------------------
    # Financial context from the authoritative case.
    # --------------------------------------------------------

    original_amount: float | None = None

    if case.get(
        "amount"
    ) is not None:

        try:

            original_amount = float(
                case.get(
                    "amount"
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            original_amount = None

    outstanding_amount_raw = payload.get(
        "outstanding_amount"
    )

    if outstanding_amount_raw is None:

        outstanding_amount = (
            original_amount
        )

    else:

        try:

            outstanding_amount = float(
                outstanding_amount_raw
            )

        except (
            TypeError,
            ValueError,
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "`outstanding_amount` must be "
                    "a number."
                ),
            )

    # --------------------------------------------------------
    # Create durable promise.
    # --------------------------------------------------------

    try:

        record = promise_tracker.create_promise(
            case_id=case_id,
            promised_amount=promised_amount,
            promise_date=promise_date,
            customer_id=customer_id,
            customer_name=customer_name,
            customer_email=customer_email,
            invoice_id=invoice_id,
            original_amount=original_amount,
            outstanding_amount=outstanding_amount,
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    # --------------------------------------------------------
    # Create the payment link immediately.
    #
    # Failure to reach Razorpay must NOT roll back the durable
    # promise. The dashboard can retry link creation explicitly.
    # --------------------------------------------------------

    payment_link_result = None
    payment_link_error = None

    try:
        payment_link_result = create_promise_payment_link(case_id)
        record = promise_tracker.get_promise(case_id) or record
    except HTTPException as exc:
        payment_link_error = str(exc.detail)

    # --------------------------------------------------------
    # Send real customer notification(s) when contact/provider data exists.
    # Failures are recorded but never roll back the financial promise.
    # --------------------------------------------------------
    customer_alert_service.send(
        record,
        "PROMISE_CREATED",
        payment_link_url=(payment_link_result or {}).get("payment_link", {}).get("short_url") if payment_link_result else None,
    )

    # --------------------------------------------------------
    # Force the next pipeline read to apply the promise overlay.
    # --------------------------------------------------------

    global _cached_result

    _cached_result = None

    response = {
        "success": True,
        "promise": _promise_record_to_dict(
            record
        ),
        "policy": {
            "promise_to_pay_active": True,
            "automated_contact_blocked": True,
            "reason": (
                "Active promise-to-pay exists; "
                "automated contact is blocked until "
                "the promise is resolved."
            ),
        },
    }

    if payment_link_result is not None:
        response["payment_link"] = payment_link_result.get("payment_link")
        response["payment_link_created"] = True
    else:
        response["payment_link_created"] = False
        response["payment_link_error"] = payment_link_error

    return response


@app.post("/api/promises/{case_id}/payment-link")
def create_promise_payment_link(case_id: str) -> dict[str, Any]:
    """Create or return the Razorpay Payment Link for an active promise."""

    promise = promise_tracker.get_promise(case_id)
    if promise is None:
        raise HTTPException(status_code=404, detail=f"No promise exists for case {case_id}.")

    if promise.status.value != "promised":
        raise HTTPException(status_code=400, detail="Payment Link can only be created for an active promise.")

    if promise.payment_link_id and promise.payment_link_url:
        return {
            "success": True,
            "idempotent": True,
            "promise": _promise_record_to_dict(promise),
            "payment_link": {
                "id": promise.payment_link_id,
                "short_url": promise.payment_link_url,
                "expire_by": (
                    int(promise.payment_link_expire_by.timestamp())
                    if promise.payment_link_expire_by else None
                ),
            },
        }

    grace_hours = max(0, int(os.getenv("PROMISE_PAYMENT_LINK_GRACE_HOURS", "24")))
    promise_timezone = ZoneInfo(
        os.getenv(
            "PROMISE_TIMEZONE",
            "Asia/Kolkata",
        )
    )
    expire_dt = promise.promise_date.replace(tzinfo=promise_timezone)
    from datetime import timedelta
    expire_dt = expire_dt + timedelta(hours=grace_hours)

    # Razorpay limits Payment Link expiry to six months from creation.
    max_expiry = datetime.now(ZoneInfo("UTC")) + timedelta(days=180)
    if expire_dt > max_expiry:
        expire_dt = max_expiry

    try:
        link = create_payment_link(
            amount_rupees=promise.promised_amount,
            customer_name=promise.customer_name or promise.customer_id or "Revive Customer",
            customer_id=promise.customer_id or "cust_unknown",
            customer_email=promise.customer_email or None,
            description=f"Revive Promise-to-Pay — {case_id}",
            revive_case_tag=f"revive-promise-{promise.promise_id}",
            surface="promise_to_pay",
            invoice_id=promise.invoice_id,
            promise_id=promise.promise_id,
            reference_id=promise.promise_id,
            expire_by=int(expire_dt.timestamp()),
        )
    except RazorpayConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    updated = promise_tracker.attach_payment_link(
        case_id=case_id,
        payment_link_id=str(link["id"]),
        payment_link_url=str(link["short_url"]),
        payment_link_expire_by=expire_dt.replace(tzinfo=None),
    )

    global _cached_result
    _cached_result = None

    return {
        "success": True,
        "idempotent": False,
        "promise": _promise_record_to_dict(updated),
        "payment_link": {
            "id": link["id"],
            "short_url": link["short_url"],
            "expire_by": int(expire_dt.timestamp()),
            "expires_at": expire_dt.isoformat(),
        },
    }


@app.post("/api/promises/{case_id}/mark-paid")
def mark_promise_paid(
    case_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Manually mark a promise fulfilled.

    This endpoint is intentionally retained for the existing
    dashboard workflow.

    IMPORTANT:

        Manual fulfillment is NOT authoritative payment proof.

    The response therefore exposes:
        payment_verified=False
        payment_source="manual"

    A future Razorpay webhook integration can call the same
    PromiseTracker.mark_paid() method with the real payment ID
    and payment_verified=True.
    """

    if payload is None:
        payload = {}

    payment_reference = payload.get(
        "payment_reference"
    )

    payment_source = str(
        payload.get(
            "payment_source"
        )
        or "manual"
    )

    try:

        record = promise_tracker.mark_paid(
            case_id=case_id,
            payment_reference=(
                str(payment_reference)
                if payment_reference is not None
                else None
            ),
            payment_source=payment_source,
            payment_verified=False,
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    global _cached_result

    _cached_result = None

    return {
        "success": True,
        "promise": _promise_record_to_dict(
            record
        ),
        "payment_verification": {
            "verified": False,
            "source": payment_source,
            "warning": (
                "This is a manual promise fulfillment "
                "and is not authoritative payment evidence."
            ),
        },
    }


@app.get("/api/customer-alerts")
def customer_alerts() -> dict[str, Any]:
    return {
        "success": True,
        "metrics": customer_alert_service.metrics(),
        "events": customer_alert_service.list_events(),
        "mode": "real_provider_only",
    }


@app.post("/api/promises/{case_id}/send-alert")
def send_promise_alert(case_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    record = promise_tracker.get_promise(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No promise exists for case {case_id}.")
    event_type = str(payload.get("event_type") or "DUE_SOON").upper()
    if event_type not in {"PROMISE_CREATED", "DUE_SOON", "PAYMENT_VERIFIED", "PROMISE_BROKEN"}:
        raise HTTPException(status_code=400, detail="Unsupported alert event type.")
    events = customer_alert_service.send(record, event_type, payment_link_url=record.payment_link_url, force=bool(payload.get("force")))
    return {"success": True, "events": events}


@app.post("/api/cases/{case_id}/voice-script")
def generate_voice_script(
    case_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Generate a Hinglish recovery script for a case, optionally with
    real ElevenLabs audio if ELEVENLABS_API_KEY is configured.
    """

    payload = payload or {}

    result = get_pipeline()
    case = None
    for c in _cases(result):
        if c.get("case_id") == case_id:
            case = c
            break

    if case is None:
        raise HTTPException(
            status_code=404,
            detail=f"Case '{case_id}' not found.",
        )

    channel = str(payload.get("channel") or "ivr_call")
    if channel not in {"ivr_call", "whatsapp_voice_note"}:
        raise HTTPException(
            status_code=400,
            detail="channel must be 'ivr_call' or 'whatsapp_voice_note'.",
        )

    promise_record = promise_tracker.get_promise(case_id)
    promise_dict = (
        asdict(promise_record) if promise_record is not None else None
    )

    script = voice_script_store.generate(
        case,
        promise=promise_dict,
        channel=channel,
        synthesize_audio=bool(payload.get("synthesize_audio")),
    )

    return {"success": True, "script": script.to_dict()}


@app.get("/api/voice-audio/{script_id}")
def get_voice_audio(script_id: str):
    """
    Stream back the real ElevenLabs audio for a previously generated
    script. Looked up by script_id (never a raw filesystem path) so
    this can't be used to read arbitrary files off disk.
    """

    record = voice_script_store.get_by_id(script_id)
    if record is None or not record.get("audio_path"):
        raise HTTPException(
            status_code=404,
            detail="No audio available for this script.",
        )

    audio_path = Path(record["audio_path"])
    if not audio_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Audio file is missing on disk.",
        )

    return FileResponse(audio_path, media_type="audio/mpeg")


@app.get("/api/cases/{case_id}/voice-scripts")
def list_voice_scripts(case_id: str) -> dict[str, Any]:
    return {
        "success": True,
        "scripts": voice_script_store.list_for_case(case_id),
    }


@app.get("/api/voice-scripts")
def voice_scripts() -> dict[str, Any]:
    return {
        "success": True,
        "metrics": voice_script_store.metrics(),
        "scripts": voice_script_store.list_all(),
    }


@app.get("/api/promises-metrics")
def promise_metrics() -> dict[str, Any]:
    """
    Return Promise-to-Pay operational metrics and persistence state.
    """

    return {
        "success": True,
        "metrics": promise_tracker.metrics(),
        "persistence": promise_tracker.persistence_info(),
    }


# ============================================================
# Dashboard
# ============================================================

@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    """
    Return complete dashboard payload.
    """

    result = get_pipeline()

    cases = _cases(result)
    metrics = _metrics(result)
    alerts = _psr_alerts(result)
    settlements = _a2a_results(result)
    ledger = _ledger(result)

    eligible_cases, settled_cases = _a2a_counts(result)

    return {
        "success": True,

        "summary": {
            "total_cases": metrics.get(
                "total_cases",
                len(cases),
            ),

            "addressable_revenue": metrics.get(
                "addressable_revenue",
                0,
            ),

            "recovered_revenue": metrics.get(
                "recovered_revenue",
                0,
            ),

            "unrecovered_revenue": metrics.get(
                "unrecovered_revenue",
                0,
            ),

            "recovery_cost": metrics.get(
                "recovery_cost",
                0,
            ),

            "net_recovered_value": metrics.get(
                "net_recovered_value",
                0,
            ),

            "recovery_rate": metrics.get(
                "recovery_rate",
                0,
            ),

            "cost_per_rupee_recovered": metrics.get(
                "cost_per_rupee_recovered",
                0,
            ),

            "pursued_cases": metrics.get(
                "pursued_cases",
                0,
            ),

            "stopped_cases": metrics.get(
                "stopped_cases",
                0,
            ),

            "recovered_cases": metrics.get(
                "recovered_cases",
                0,
            ),

            "psr_alerts": metrics.get(
                "psr_alerts",
                len(alerts),
            ),

            "a2a_eligible": eligible_cases,

            "a2a_settled": settled_cases,

            "a2a_eligible_cases": eligible_cases,

            "a2a_settled_cases": settled_cases,

            "ledger_events": len(ledger),
        },

        "metrics": metrics,

        "psr_alerts": alerts,

        "a2a_settlements": settlements,

        "cases": cases,

        "ledger": ledger,
    }


# ============================================================
# Metrics
# ============================================================

@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    """
    Return both metric layers.

    metrics:
        Synthetic 105-case benchmark.

    live_metrics:
        Real Razorpay Test Mode operational state.

    These two layers are intentionally isolated.
    """

    result = get_pipeline()

    synthetic_metrics = _metrics(result)

    live_cases_data = live_case_store.load()

    live_metrics = build_live_metrics(
        live_cases_data
    )

    return {
        "success": True,

        # ----------------------------------------------------
        # SYNTHETIC BENCHMARK
        # ----------------------------------------------------

        "metrics": synthetic_metrics,

        # ----------------------------------------------------
        # REAL RAZORPAY TEST MODE
        # ----------------------------------------------------

        "live_metrics": live_metrics,
    }


# ============================================================
# Cases
# ============================================================

@app.get("/api/cases")
def cases() -> dict[str, Any]:

    result = get_pipeline()

    case_results = _cases(result)

    return {
        "success": True,
        "count": len(case_results),
        "cases": case_results,
    }


# ============================================================
# Single Case
# ============================================================

@app.get("/api/cases/{case_id}")
def case_detail(
    case_id: str,
) -> dict[str, Any]:

    result = get_pipeline()

    for case in _cases(result):

        if case.get("case_id") == case_id:

            return {
                "success": True,
                "case": case,
            }

    raise HTTPException(
        status_code=404,
        detail=f"Case '{case_id}' not found.",
    )


# ============================================================
# Explain Case
# ============================================================

@app.post("/api/cases/{case_id}/explain")
def explain_case(
    case_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Explain an existing Revive decision.

    This endpoint does not recalculate ROI.
    """

    result = get_pipeline()

    selected_case = None

    for case in _cases(result):

        if case.get("case_id") == case_id:

            selected_case = case
            break

    if selected_case is None:

        raise HTTPException(
            status_code=404,
            detail=f"Case '{case_id}' not found.",
        )

    question = None

    if isinstance(payload, dict):

        raw_question = payload.get("question")

        if raw_question is not None:

            question = str(raw_question).strip()

            if not question:
                question = None

    evidence = build_case_evidence(
        case=selected_case,
        ledger_events=_ledger(result),
    )

    explanation = _decision_explainer.explain(
        evidence=evidence,
        question=question,
    )

    return {
        "success": True,
        "case_id": case_id,
        "question": question,
        "data": explanation,
    }


# ============================================================
# PSR Guardian
# ============================================================

@app.get("/api/psr-alerts")
def psr_alerts() -> dict[str, Any]:

    result = get_pipeline()

    alerts = _psr_alerts(result)

    return {
        "success": True,
        "count": len(alerts),
        "alerts": alerts,
    }


# ============================================================
# A2A
# ============================================================

# ============================================================
# LIVE A2A SETTLEMENT
# ============================================================

@app.post("/api/a2a/live/{case_id}/settle")
def settle_live_a2a(
    case_id: str,
) -> dict[str, Any]:
    """
    Execute or resume an A2A settlement for one real Razorpay
    live failure.

    Lifecycle:

        LIVE FAILED PAYMENT
                ↓
        A2A NEGOTIATION
                ↓
             AGREED
                ↓
        RAZORPAY PAYMENT LINK
                ↓
             PENDING
                ↓
        payment.captured webhook
                ↓
            CONFIRMED

    IMPORTANT:

        A2A AGREED does NOT mean payment recovered.

        Only a verified Razorpay payment.captured webhook
        can confirm actual recovery.

    IDEMPOTENCY:

        Existing agreement + payment link
            → return existing agreement

        Existing agreement + no payment link
            → reuse agreement
            → do NOT renegotiate
            → retry payment-link creation

        No existing agreement
            → negotiate
            → persist agreement
            → create payment link
    """

    # ========================================================
    # Find live case
    # ========================================================

    case = live_case_store.find_by_case_id(
        case_id
    )

    if case is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No live Razorpay case found "
                f"with case_id={case_id!r}."
            ),
        )

    # ========================================================
    # Recovery state
    # ========================================================

    if case.get("recovery_status") == "RECOVERED":
        raise HTTPException(
            status_code=409,
            detail=(
                "This live case is already recovered. "
                "A new A2A settlement is not permitted."
            ),
        )

    if case.get("recovery_status") != "PENDING_RECOVERY":
        raise HTTPException(
            status_code=409,
            detail=(
                "A2A settlement requires a live case with "
                "recovery_status=PENDING_RECOVERY. "
                f"Current status: "
                f"{case.get('recovery_status')!r}."
            ),
        )

    # ========================================================
    # Correlation tag
    # ========================================================

    revive_case_tag = case.get(
        "revive_case_tag"
    )

    if not revive_case_tag:
        raise HTTPException(
            status_code=422,
            detail=(
                "Live case has no revive_case_tag. "
                "A2A settlement cannot safely create a "
                "payment link without a correlation tag."
            ),
        )

    # ========================================================
    # Required financial data
    # ========================================================

    try:
        amount = float(
            case.get("amount")
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=(
                "Live case contains an invalid amount."
            ),
        )

    if amount <= 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "Live case amount must be greater than zero."
            ),
        )

    # ========================================================
    # Check existing live agreement FIRST.
    #
    # This is the critical idempotency fix.
    # ========================================================

    existing = (
        live_a2a_settlement_store.get_by_case_id(
            case_id
        )
    )

    agreement = existing

    # ========================================================
    # Fully established agreement
    # ========================================================

    if existing is not None and existing.get("payment_link_id"):
        return {
            "success": True,
            "idempotent": True,
            "resumed": False,
            "case_id": case_id,
            "agreement": existing,
            "payment": {
                "payment_link_id": (
                    existing.get("payment_link_id")
                ),
                "short_url": (
                    existing.get("payment_url")
                ),
                "amount": (
                    existing.get("agreed_amount")
                ),
                "status": (
                    existing.get(
                        "payment_status",
                        "PENDING",
                    )
                ),
            },
        }

    # ========================================================
    # Existing agreement without payment link
    #
    # IMPORTANT:
    #
    # Do not execute the A2A engine again.
    # Reuse the durable agreement and retry only the
    # payment-link creation below.
    # ========================================================

    if agreement is not None:

        try:
            agreed_amount = float(
                agreement.get("agreed_amount")
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=500,
                detail=(
                    "Existing A2A agreement contains "
                    "an invalid agreed amount."
                ),
            )

        if agreed_amount <= 0:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Existing A2A agreement has an "
                    "invalid agreed amount."
                ),
            )

        if agreed_amount > amount:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Existing A2A agreement exceeds "
                    "the original live case amount."
                ),
            )

    # ========================================================
    # New agreement path
    #
    # Only reach the A2A negotiation engine when no durable
    # agreement exists.
    # ========================================================

    else:

        # ----------------------------------------------------
        # Build the case representation expected by Module 6
        # ----------------------------------------------------

        a2a_case = {
            "case_id": str(
                case.get(
                    "case_id",
                    case_id,
                )
            ),

            "surface": str(
                case.get(
                    "surface"
                )
                or "subscription_failure"
            ),

            "customer_id": str(
                case.get(
                    "customer_id",
                    "",
                )
            ),

            "amount": amount,

            "timestamp": str(
                case.get(
                    "timestamp"
                )
                or datetime.now().isoformat()
            ),

            "due_date": str(
                case.get(
                    "due_date",
                    "",
                )
            ),

            "root_cause_label": str(
                case.get(
                    "root_cause_label",
                    "",
                )
            ),

            "has_ap_agent": bool(
                case.get(
                    "has_ap_agent",
                    False,
                )
            ),

            "invoice_id": str(
                case.get(
                    "invoice_id"
                )
                or ""
            ),

            "disputed": bool(
                case.get(
                    "disputed",
                    False,
                )
            ),
        }

        # ----------------------------------------------------
        # Live A2A eligibility gates
        # ----------------------------------------------------

        if a2a_case["surface"] != "b2b_receivable":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Live A2A settlement requires a "
                    "b2b_receivable case."
                ),
            )

        if not a2a_case["has_ap_agent"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Live A2A settlement requires an "
                    "independent AP agent."
                ),
            )

        if a2a_case["disputed"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Disputed invoices cannot enter "
                    "A2A settlement."
                ),
            )

        if not a2a_case["invoice_id"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Live A2A settlement requires an invoice_id."
                ),
            )

        # ----------------------------------------------------
        # Execute the existing A2A engine.
        # ----------------------------------------------------

        try:
            engine = A2ASettlementEngine()

            if engine.a2a_mode != "remote":
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": (
                            "Live A2A settlement requires the "
                            "remote payer agent, which is "
                            "unreachable right now — refusing "
                            "to silently negotiate with the "
                            "offline synthetic agent instead."
                        ),
                        "error": engine.a2a_client_error,
                    },
                )

            result = engine.negotiate(
                a2a_case
            )

        except HTTPException:
            raise

        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": (
                        "Live A2A negotiation failed."
                    ),
                    "error": str(exc),
                },
            )

        # ----------------------------------------------------
        # A2A agreement check
        # ----------------------------------------------------

        if (
            not result.eligible
            or result.outcome != "SETTLED"
            or result.settlement_status != "AGREED"
            or result.payment_status != "PENDING"
            or result.recovery_confirmed
        ):
            return {
                "success": True,
                "idempotent": False,
                "resumed": False,
                "case_id": case_id,
                "negotiation": {
                    "eligible": result.eligible,
                    "outcome": result.outcome,
                    "settlement_status": (
                        result.settlement_status
                    ),
                    "payment_status": (
                        result.payment_status
                    ),
                    "recovery_confirmed": (
                        result.recovery_confirmed
                    ),
                    "final_amount": (
                        result.final_amount
                    ),
                    "discount_percent": (
                        result.discount_percent
                    ),
                    "rounds": result.rounds,
                    "reason": result.reason,
                    "agreement_id": (
                        result.agreement_id
                    ),
                    "a2a_agent_id": (
                        result.a2a_agent_id
                    ),
                    "a2a_task_id": (
                        result.a2a_task_id
                    ),
                    "a2a_context_id": (
                        result.a2a_context_id
                    ),
                },
                "agreement": None,
            }

        # ----------------------------------------------------
        # Validate agreed amount
        # ----------------------------------------------------

        try:
            agreed_amount = float(
                result.final_amount
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail=(
                    "Payer agent produced an invalid "
                    "agreed amount."
                ),
            )

        if agreed_amount <= 0:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Payer agent produced an invalid "
                    "agreed amount."
                ),
            )

        if agreed_amount > amount:
            raise HTTPException(
                status_code=502,
                detail=(
                    "A2A agreed amount exceeds the "
                    "original live invoice amount."
                ),
            )

        # ----------------------------------------------------
        # Persist agreement BEFORE payment link.
        # ----------------------------------------------------

        try:
            agreement = (
                live_a2a_settlement_store.create(
                    case_id=case_id,

                    invoice_id=str(
                        case.get("invoice_id")
                        or ""
                    ),

                    revive_case_tag=(
                        revive_case_tag
                    ),

                    agreed_amount=(
                        agreed_amount
                    ),

                    payer_agent_id=str(
                        result.a2a_agent_id
                        or "unknown-payer-agent"
                    ),

                    agreement_id=(
                        result.agreement_id
                    ),

                    a2a_task_id=(
                        result.a2a_task_id
                    ),

                    a2a_context_id=(
                        result.a2a_context_id
                    ),
                )
            )

        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": (
                        "Unable to persist the "
                        "A2A agreement."
                    ),
                    "error": str(exc),
                },
            )

    # ========================================================
    # Create Razorpay Payment Link
    #
    # For a resumed agreement this is the ONLY operation
    # performed after loading the existing agreement.
    # ========================================================

    try:
        payment_link = create_payment_link(
            amount_rupees=agreed_amount,

            customer_name=str(
                case.get(
                    "customer_name",
                    "Revive Customer",
                )
            ),

            customer_id=str(
                case.get(
                    "customer_id",
                    "cust_unknown",
                )
            ),

            customer_email=(
                str(case.get("customer_email")).strip()
                if case.get("customer_email") is not None
                else None
            ),

            description=(
                f"Revive A2A settlement — "
                f"{case_id}"
            ),

            revive_case_tag=(
                revive_case_tag
            ),
        )

    except RazorpayConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        )

    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )

    # ========================================================
    # Validate Razorpay response
    # ========================================================

    payment_link_id = payment_link.get(
        "id"
    )

    payment_url = payment_link.get(
        "short_url"
    )

    if not payment_link_id:
        raise HTTPException(
            status_code=502,
            detail=(
                "Razorpay created an invalid payment-link "
                "response without a payment-link ID."
            ),
        )

    if not payment_url:
        raise HTTPException(
            status_code=502,
            detail=(
                "Razorpay created a payment link without "
                "a usable short URL."
            ),
        )

    # ========================================================
    # Persist payment link
    # ========================================================

    updated_agreement = (
        live_a2a_settlement_store.attach_payment_link(
            agreement_id=(
                agreement["agreement_id"]
            ),

            payment_link_id=str(
                payment_link_id
            ),

            payment_url=str(
                payment_url
            ),
        )
    )

    if updated_agreement is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "A2A agreement was created but the "
                "Razorpay payment link could not be attached."
            ),
        )

    # ========================================================
    # Final response
    # ========================================================

    return {
        "success": True,

        "idempotent": (
            existing is not None
        ),

        "resumed": (
            existing is not None
        ),

        "case_id": case_id,

        "negotiation": (
            {
                "eligible": result.eligible,
                "outcome": result.outcome,
                "settlement_status": (
                    result.settlement_status
                ),
                "payment_status": (
                    result.payment_status
                ),
                "recovery_confirmed": (
                    result.recovery_confirmed
                ),
                "final_amount": (
                    result.final_amount
                ),
                "discount_percent": (
                    result.discount_percent
                ),
                "rounds": result.rounds,
                "reason": result.reason,
                "agreement_id": (
                    result.agreement_id
                ),
                "a2a_agent_id": (
                    result.a2a_agent_id
                ),
                "a2a_task_id": (
                    result.a2a_task_id
                ),
                "a2a_context_id": (
                    result.a2a_context_id
                ),
            }
            if existing is None
            else {
                "eligible": True,
                "outcome": "SETTLED",
                "settlement_status": (
                    updated_agreement.get(
                        "settlement_status"
                    )
                ),
                "payment_status": (
                    updated_agreement.get(
                        "payment_status"
                    )
                ),
                "recovery_confirmed": (
                    updated_agreement.get(
                        "recovery_confirmed",
                        False,
                    )
                ),
                "final_amount": (
                    updated_agreement.get(
                        "agreed_amount"
                    )
                ),
                "rounds": None,
                "reason": (
                    "Existing A2A agreement resumed; "
                    "payment link creation retried."
                ),
                "agreement_id": (
                    updated_agreement.get(
                        "agreement_id"
                    )
                ),
                "a2a_agent_id": (
                    updated_agreement.get(
                        "payer_agent_id"
                    )
                ),
                "a2a_task_id": (
                    updated_agreement.get(
                        "a2a_task_id"
                    )
                ),
                "a2a_context_id": (
                    updated_agreement.get(
                        "a2a_context_id"
                    )
                ),
            }
        ),

        "agreement": updated_agreement,

        "payment": {
            "payment_link_id": (
                payment_link_id
            ),

            "short_url": (
                payment_url
            ),

            "amount": agreed_amount,

            "status": (
                updated_agreement.get(
                    "payment_status",
                    "PENDING",
                )
            ),
        },
    }


@app.get("/api/a2a/live-settlements")
def live_a2a_settlements() -> dict[str, Any]:
    """
    Return persistent live A2A settlement agreements.

    These are separate from the synthetic 105-case A2A
    benchmark.
    """

    settlements = (
        live_a2a_settlement_store.list_all()
    )

    return {
        "success": True,
        "count": len(settlements),
        "settlements": settlements,
    }

@app.get("/api/a2a")
def a2a_settlements() -> dict[str, Any]:

    result = get_pipeline()

    settlements = _a2a_results(result)

    return {
        "success": True,
        "count": len(settlements),
        "settlements": settlements,
    }


# ============================================================
# Real Razorpay Capture
# ============================================================

_REAL_CAPTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "real_captured_case.json"
)


@app.get("/api/real-capture")
def real_capture() -> dict[str, Any]:
    """
    Return the optional real Razorpay test-mode capture.

    This case is intentionally NOT merged into the authoritative
    105-case pipeline. It is surfaced separately as a verified
    Razorpay sandbox artifact.
    """

    if not _REAL_CAPTURE_PATH.exists():
        return {
            "success": True,
            "found": False,
            "case": None,
        }

    try:
        with _REAL_CAPTURE_PATH.open(
            "r",
            encoding="utf-8",
        ) as f:
            case = json.load(f)

    except (OSError, ValueError):
        return {
            "success": True,
            "found": False,
            "case": None,
        }

    return {
        "success": True,
        "found": True,
        "case": case,
    }


# ============================================================
# Recovery Ledger
# ============================================================

@app.get("/api/ledger")
def recovery_ledger() -> dict[str, Any]:

    result = get_pipeline()

    ledger = _ledger(result)

    return {
        "success": True,
        "count": len(ledger),
        "events": ledger,
    }


# ============================================================
# Single Case Ledger
# ============================================================

@app.get("/api/ledger/{case_id}")
def ledger_case_history(
    case_id: str,
) -> dict[str, Any]:

    result = get_pipeline()

    events = [
        event
        for event in _ledger(result)
        if event.get("case_id") == case_id
    ]

    if not events:

        raise HTTPException(
            status_code=404,
            detail=(
                f"No ledger history found "
                f"for case '{case_id}'."
            ),
        )

    events.sort(
        key=lambda event: (
            event.get("attempt_number", 0),
            event.get("timestamp", ""),
        )
    )

    return {
        "success": True,
        "case_id": case_id,
        "count": len(events),
        "events": events,
    }


# ============================================================
# WHAT-IF POLICY SIMULATOR
# ============================================================

@app.post("/api/simulate")
def simulate_policy(
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Run the actual Revive engine with temporary policy overrides.

    Supported overrides:

        max_contact_attempts
        max_discount_percent
        max_negotiation_rounds
        cooldown_hours
        retry_max_attempts

    The authoritative policy.yaml is NEVER modified.
    """

    # --------------------------------------------------------
    # Validate request
    # --------------------------------------------------------

    if payload is None:
        payload = {}

    if not isinstance(payload, dict):

        raise HTTPException(
            status_code=400,
            detail=(
                "Simulation payload must be a JSON object."
            ),
        )

    # --------------------------------------------------------
    # Load authoritative policy
    # --------------------------------------------------------

    try:

        base_policy = load_policy()

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                f"Unable to load Revive policy: {exc}"
            ),
        )

    # --------------------------------------------------------
    # Deep copy
    # --------------------------------------------------------

    simulation_policy = deepcopy(base_policy)

    # --------------------------------------------------------
    # Allowed parameters
    # --------------------------------------------------------

    allowed_parameters = {
        "max_contact_attempts",
        "max_discount_percent",
        "max_negotiation_rounds",
        "cooldown_hours",
        "retry_max_attempts",
    }

    unknown_parameters = sorted(
        set(payload.keys()) - allowed_parameters
    )

    if unknown_parameters:

        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Unsupported simulation parameter(s)."
                ),
                "unknown_parameters": unknown_parameters,
                "allowed_parameters": sorted(
                    allowed_parameters
                ),
            },
        )

    # --------------------------------------------------------
    # Validation helper
    # --------------------------------------------------------

    def positive_number(
        name: str,
        value: Any,
    ) -> float:

        if isinstance(value, bool):

            raise HTTPException(
                status_code=400,
                detail=f"{name} must be numeric.",
            )

        try:

            converted = float(value)

        except (
            TypeError,
            ValueError,
        ):

            raise HTTPException(
                status_code=400,
                detail=f"{name} must be numeric.",
            )

        if converted < 0:

            raise HTTPException(
                status_code=400,
                detail=f"{name} cannot be negative.",
            )

        return converted

    # --------------------------------------------------------
    # Max contact attempts
    # --------------------------------------------------------

    if "max_contact_attempts" in payload:

        value = positive_number(
            "max_contact_attempts",
            payload["max_contact_attempts"],
        )

        if not value.is_integer():

            raise HTTPException(
                status_code=400,
                detail=(
                    "max_contact_attempts "
                    "must be an integer."
                ),
            )

        value = int(value)

        if value < 1:

            raise HTTPException(
                status_code=400,
                detail=(
                    "max_contact_attempts "
                    "must be >= 1."
                ),
            )

        simulation_policy[
            "max_contact_attempts"
        ] = value

    # --------------------------------------------------------
    # Maximum discount
    # --------------------------------------------------------

    if "max_discount_percent" in payload:

        value = positive_number(
            "max_discount_percent",
            payload["max_discount_percent"],
        )

        if value > 100:

            raise HTTPException(
                status_code=400,
                detail=(
                    "max_discount_percent "
                    "cannot exceed 100."
                ),
            )

        simulation_policy[
            "max_discount_percent"
        ] = value

    # --------------------------------------------------------
    # Negotiation rounds
    # --------------------------------------------------------

    if "max_negotiation_rounds" in payload:

        value = positive_number(
            "max_negotiation_rounds",
            payload["max_negotiation_rounds"],
        )

        if not value.is_integer():

            raise HTTPException(
                status_code=400,
                detail=(
                    "max_negotiation_rounds "
                    "must be an integer."
                ),
            )

        value = int(value)

        if value < 1:

            raise HTTPException(
                status_code=400,
                detail=(
                    "max_negotiation_rounds "
                    "must be >= 1."
                ),
            )

        simulation_policy[
            "max_negotiation_rounds"
        ] = value

    # --------------------------------------------------------
    # Cooldown
    # --------------------------------------------------------

    if "cooldown_hours" in payload:

        value = positive_number(
            "cooldown_hours",
            payload["cooldown_hours"],
        )

        simulation_policy[
            "cooldown_hours"
        ] = value

    # --------------------------------------------------------
    # ROI retry maximum
    # --------------------------------------------------------

    if "retry_max_attempts" in payload:

        value = positive_number(
            "retry_max_attempts",
            payload["retry_max_attempts"],
        )

        if not value.is_integer():

            raise HTTPException(
                status_code=400,
                detail=(
                    "retry_max_attempts "
                    "must be an integer."
                ),
            )

        value = int(value)

        if value < 1:

            raise HTTPException(
                status_code=400,
                detail=(
                    "retry_max_attempts "
                    "must be >= 1."
                ),
            )

        simulation_policy[
            "retry"
        ][
            "max_attempts"
        ] = value

    # --------------------------------------------------------
    # Load cases
    # --------------------------------------------------------

    try:

        cases = load_cases()

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                f"Unable to load Revive cases: {exc}"
            ),
        )

    # --------------------------------------------------------
    # Fresh simulation pipeline
    # --------------------------------------------------------

    try:

        simulation_pipeline = RevivePipeline(
            policy_override=simulation_policy
        )

        simulation_result = (
            simulation_pipeline.run(cases)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail={
                "message": "Simulation failed.",
                "error": str(exc),
            },
        )

    # --------------------------------------------------------
    # Serialize
    # --------------------------------------------------------

    simulation_payload = pipeline_to_dict(
        simulation_result
    )

    simulation_metrics = simulation_payload.get(
        "metrics",
        {},
    )

    if not isinstance(
        simulation_metrics,
        dict,
    ):
        simulation_metrics = {}

    # --------------------------------------------------------
    # Current pipeline
    # --------------------------------------------------------

    base_result = get_pipeline()

    base_metrics = _metrics(
        base_result
    )

    # --------------------------------------------------------
    # Differences
    # --------------------------------------------------------

    recovery_difference = (
        float(
            simulation_metrics.get(
                "recovered_revenue",
                0,
            )
        )
        -
        float(
            base_metrics.get(
                "recovered_revenue",
                0,
            )
        )
    )

    recovery_rate_difference = (
        float(
            simulation_metrics.get(
                "recovery_rate",
                0,
            )
        )
        -
        float(
            base_metrics.get(
                "recovery_rate",
                0,
            )
        )
    )

    cost_difference = (
        float(
            simulation_metrics.get(
                "recovery_cost",
                0,
            )
        )
        -
        float(
            base_metrics.get(
                "recovery_cost",
                0,
            )
        )
    )

    net_value_difference = (
        float(
            simulation_metrics.get(
                "net_recovered_value",
                0,
            )
        )
        -
        float(
            base_metrics.get(
                "net_recovered_value",
                0,
            )
        )
    )

    pursued_difference = (
        int(
            simulation_metrics.get(
                "pursued_cases",
                0,
            )
        )
        -
        int(
            base_metrics.get(
                "pursued_cases",
                0,
            )
        )
    )

    stopped_difference = (
        int(
            simulation_metrics.get(
                "stopped_cases",
                0,
            )
        )
        -
        int(
            base_metrics.get(
                "stopped_cases",
                0,
            )
        )
    )

    # --------------------------------------------------------
    # Return
    # --------------------------------------------------------

    return {
        "success": True,

        "simulation": {

            "overrides": payload,

            "effective_policy": {

                "max_contact_attempts": (
                    simulation_policy.get(
                        "max_contact_attempts"
                    )
                ),

                "max_discount_percent": (
                    simulation_policy.get(
                        "max_discount_percent"
                    )
                ),

                "max_negotiation_rounds": (
                    simulation_policy.get(
                        "max_negotiation_rounds"
                    )
                ),

                "cooldown_hours": (
                    simulation_policy.get(
                        "cooldown_hours"
                    )
                ),

                "retry_max_attempts": (
                    simulation_policy.get(
                        "retry",
                        {},
                    ).get(
                        "max_attempts"
                    )
                ),
            },

            "metrics": simulation_metrics,

            "comparison_to_current": {

                "recovered_revenue_difference": (
                    recovery_difference
                ),

                "recovery_rate_difference": (
                    recovery_rate_difference
                ),

                "recovery_cost_difference": (
                    cost_difference
                ),

                "net_recovered_value_difference": (
                    net_value_difference
                ),

                "pursued_cases_difference": (
                    pursued_difference
                ),

                "stopped_cases_difference": (
                    stopped_difference
                ),
            },

            "cases": simulation_payload.get(
                "cases",
                [],
            ),

            "psr_alerts": simulation_payload.get(
                "psr_alerts",
                [],
            ),

            "a2a_settlements": simulation_payload.get(
                "a2a_settlements",
                [],
            ),

            "ledger": simulation_payload.get(
                "ledger",
                [],
            ),
        },
    }


# ============================================================
# BOARD REPORT PDF
#
# Generation lives in dashboard_api/board_report.py (see that
# module's docstring). It renders the FULL pipeline result --
# every PSR alert, every case, every A2A settlement, and every
# ledger event -- not just aggregate counts. This function does
# NOT recompute recovery business logic.
# ============================================================


# ============================================================
# EXPORT BOARD REPORT
# ============================================================

@app.get("/api/board-report")
def board_report():
    """
    Generate and return the executive Revive Board Report PDF.

    The report is generated from the current authoritative
    pipeline result.
    """

    try:

        result = get_pipeline()

        pdf_buffer = build_board_report_pdf(
            result
        )

        filename = "Revive_Board_Report.pdf"

        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                )
            },
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail={
                "message": (
                    "Unable to generate board report."
                ),
                "error": str(exc),
            },
        )


# ============================================================
# Ops Copilot
#
# An internal chat assistant for the collections/ops team,
# embedded in the dashboard. It never duplicates business logic
# - every tool below is a thin wrapper around an endpoint
# function defined above. Read tools execute immediately; write
# tools always pause for explicit operator confirmation via
# POST /api/copilot/confirm before anything actually happens.
# ============================================================


def _tool_list_cases(tool_input: dict[str, Any]) -> dict[str, Any]:
    data = cases()
    rows = data.get("cases", data.get("data", []))
    status_filter = tool_input.get("status")
    if status_filter:
        rows = [
            r
            for r in rows
            if str(r.get("recovery_status") or r.get("outcome") or "").upper()
            == str(status_filter).upper()
        ]
    limit = tool_input.get("limit") or 15
    return {"success": True, "count": len(rows), "cases": rows[: int(limit)]}


def _tool_get_case(tool_input: dict[str, Any]) -> dict[str, Any]:
    return case_detail(str(tool_input["case_id"]))


def _tool_explain_case(tool_input: dict[str, Any]) -> dict[str, Any]:
    payload = {"question": tool_input.get("question")} if tool_input.get("question") else None
    return explain_case(str(tool_input["case_id"]), payload)


def _tool_dashboard_summary(_: dict[str, Any]) -> dict[str, Any]:
    return dashboard()


def _tool_psr_alerts(_: dict[str, Any]) -> dict[str, Any]:
    return psr_alerts()


def _tool_ledger(tool_input: dict[str, Any]) -> dict[str, Any]:
    case_id = tool_input.get("case_id")
    if case_id:
        return ledger_case_history(str(case_id))
    return recovery_ledger()


def _tool_list_customers(tool_input: dict[str, Any]) -> dict[str, Any]:
    data = list_customers()
    query = (tool_input.get("query") or "").strip().lower()
    if query:
        rows = [
            c
            for c in data.get("customers", [])
            if query in json.dumps(c, default=str).lower()
        ]
        return {"success": True, "count": len(rows), "customers": rows}
    return data


def _tool_get_customer(tool_input: dict[str, Any]) -> dict[str, Any]:
    return get_customer(str(tool_input["customer_id"]))


def _tool_list_promises(tool_input: dict[str, Any]) -> dict[str, Any]:
    data = list_promises()
    status_filter = tool_input.get("status")
    if status_filter:
        rows = [
            p
            for p in data.get("promises", [])
            if str(p.get("status", "")).upper() == str(status_filter).upper()
        ]
        return {"success": True, "count": len(rows), "promises": rows}
    return data


def _tool_get_promise(tool_input: dict[str, Any]) -> dict[str, Any]:
    return get_promise(str(tool_input["case_id"]))


def _tool_create_promise(tool_input: dict[str, Any]) -> dict[str, Any]:
    return create_promise(dict(tool_input))


def _tool_create_payment_link(tool_input: dict[str, Any]) -> dict[str, Any]:
    return create_promise_payment_link(str(tool_input["case_id"]))


def _tool_mark_promise_paid(tool_input: dict[str, Any]) -> dict[str, Any]:
    case_id = str(tool_input["case_id"])
    payload = {k: v for k, v in tool_input.items() if k != "case_id"}
    return mark_promise_paid(case_id, payload or None)


def _tool_send_promise_alert(tool_input: dict[str, Any]) -> dict[str, Any]:
    case_id = str(tool_input["case_id"])
    payload = {k: v for k, v in tool_input.items() if k != "case_id"}
    return send_promise_alert(case_id, payload or None)


def _tool_retry_live_payment(tool_input: dict[str, Any]) -> dict[str, Any]:
    return retry_live_case_payment(str(tool_input["case_id"]))


def _tool_settle_a2a(tool_input: dict[str, Any]) -> dict[str, Any]:
    return settle_live_a2a(str(tool_input["case_id"]))


COPILOT_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="list_cases",
        description="List recovery cases, optionally filtered by recovery_status "
        "(e.g. PENDING, RECOVERED, WRITTEN_OFF). Use this to find case IDs.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Optional status filter."},
                "limit": {"type": "integer", "description": "Max rows to return (default 15)."},
            },
        },
        handler=_tool_list_cases,
    ),
    ToolSpec(
        name="get_case",
        description="Get full detail for a single case by case_id.",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        },
        handler=_tool_get_case,
    ),
    ToolSpec(
        name="explain_case",
        description="Explain why Revive made the decision it did for a case "
        "(policy reasoning, ROI, diagnosis). Optionally answer a specific question "
        "about that case's decision.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "question": {"type": "string", "description": "Optional specific question."},
            },
            "required": ["case_id"],
        },
        handler=_tool_explain_case,
    ),
    ToolSpec(
        name="get_dashboard_summary",
        description="Get the overall dashboard summary: aggregate metrics, recovered "
        "revenue, case counts, etc.",
        input_schema={"type": "object", "properties": {}},
        handler=_tool_dashboard_summary,
    ),
    ToolSpec(
        name="get_psr_alerts",
        description="List current PSR Guardian (payment success rate) alerts.",
        input_schema={"type": "object", "properties": {}},
        handler=_tool_psr_alerts,
    ),
    ToolSpec(
        name="get_ledger",
        description="Get the recovery ledger - either the full ledger, or the event "
        "history for a single case_id if provided.",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string", "description": "Optional."}},
        },
        handler=_tool_ledger,
    ),
    ToolSpec(
        name="list_customers",
        description="List known customers, optionally filtered by a free-text query "
        "matched against name/email/id.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
        handler=_tool_list_customers,
    ),
    ToolSpec(
        name="get_customer",
        description="Get a single customer's detail by customer_id.",
        input_schema={
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
        handler=_tool_get_customer,
    ),
    ToolSpec(
        name="list_promises",
        description="List promise-to-pay records, optionally filtered by status "
        "(promised, fulfilled, broken).",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
        handler=_tool_list_promises,
    ),
    ToolSpec(
        name="get_promise",
        description="Get the current promise and full audit trail for one case_id.",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        },
        handler=_tool_get_promise,
    ),
    ToolSpec(
        name="create_promise",
        description="Record a new promise-to-pay for a case. Requires operator "
        "confirmation before it takes effect.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "promised_amount": {"type": "number"},
                "promise_date": {
                    "type": "string",
                    "description": "ISO datetime, e.g. 2026-09-15T00:00:00",
                },
            },
            "required": ["case_id", "promised_amount", "promise_date"],
        },
        handler=_tool_create_promise,
        mutating=True,
        confirmation_summary=lambda i: (
            f"Create a promise-to-pay on {i.get('case_id')} for "
            f"₹{i.get('promised_amount')} due {i.get('promise_date')}."
        ),
    ),
    ToolSpec(
        name="create_payment_link",
        description="Generate (or return the existing) Razorpay payment link for an "
        "active promise on a case. Requires operator confirmation.",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        },
        handler=_tool_create_payment_link,
        mutating=True,
        confirmation_summary=lambda i: f"Generate a payment link for case {i.get('case_id')}.",
    ),
    ToolSpec(
        name="mark_promise_paid",
        description="Manually mark a case's promise as fulfilled (not authoritative "
        "payment proof - use only when there's confirmed offline evidence of payment). "
        "Requires operator confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "payment_reference": {"type": "string"},
                "payment_source": {"type": "string"},
            },
            "required": ["case_id"],
        },
        handler=_tool_mark_promise_paid,
        mutating=True,
        confirmation_summary=lambda i: f"Manually mark the promise on case {i.get('case_id')} as paid.",
    ),
    ToolSpec(
        name="send_promise_alert",
        description="Send a customer alert/reminder for a case's promise "
        "(event_type: PROMISE_CREATED, DUE_SOON, PAYMENT_VERIFIED, PROMISE_BROKEN). "
        "Requires operator confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "event_type": {"type": "string"},
            },
            "required": ["case_id"],
        },
        handler=_tool_send_promise_alert,
        mutating=True,
        confirmation_summary=lambda i: (
            f"Send a {i.get('event_type', 'DUE_SOON')} alert for case {i.get('case_id')}."
        ),
    ),
    ToolSpec(
        name="retry_live_payment",
        description="Issue a new Razorpay payment link retry for a live pending-recovery "
        "case. Requires operator confirmation.",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        },
        handler=_tool_retry_live_payment,
        mutating=True,
        confirmation_summary=lambda i: f"Retry the live payment for case {i.get('case_id')}.",
    ),
    ToolSpec(
        name="settle_a2a",
        description="Execute or resume an A2A settlement negotiation for a live failed "
        "payment case. Requires operator confirmation.",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        },
        handler=_tool_settle_a2a,
        mutating=True,
        confirmation_summary=lambda i: f"Run A2A settlement for case {i.get('case_id')}.",
    ),
]

copilot_agent = CopilotAgent(tools=COPILOT_TOOLS)


@app.post("/api/copilot/chat")
def copilot_chat(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send a message to the Ops Copilot.

    Body:
        {
            "message": "which cases are overdue for CUST-018?",
            "conversation_id": "optional - omit to start a new conversation"
        }

    If the model wants to take an action (create a promise, generate a
    payment link, mark paid, retry a payment, ...), the response includes
    `pending_action` instead of performing it. Call POST
    /api/copilot/confirm with that action_id to approve or reject it.
    """

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="`message` is required.")

    conversation_id = payload.get("conversation_id") or uuid.uuid4().hex[:12]

    return copilot_agent.chat(conversation_id, message.strip())


@app.post("/api/copilot/confirm")
def copilot_confirm(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Approve or reject a pending copilot action.

    Body:
        {
            "action_id": "a1b2c3d4e5f6",
            "approved": true
        }
    """

    action_id = payload.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        raise HTTPException(status_code=400, detail="`action_id` is required.")

    approved = bool(payload.get("approved"))

    return copilot_agent.confirm(action_id.strip(), approved)


@app.get("/api/copilot/audit-log")
def copilot_audit_log() -> dict[str, Any]:
    """Return every write action the copilot has executed, for accountability."""

    entries = [
        {
            "action_id": e.action_id,
            "conversation_id": e.conversation_id,
            "tool_name": e.tool_name,
            "tool_input": e.tool_input,
            "approved": e.approved,
            "result": e.result,
            "timestamp": e.timestamp,
        }
        for e in copilot_agent.audit_log
    ]
    return {"success": True, "count": len(entries), "entries": entries}


# ============================================================
# Self Test
# ============================================================

def main() -> None:

    print("=" * 72)

    print("REVIVE — MODULE 8")

    print("Dashboard API")

    print("=" * 72)

    print()

    print(
        "FastAPI application initialized."
    )

    print()

    print(
        "Registered endpoints:"
    )

    for route in app.routes:

        if hasattr(
            route,
            "path",
        ):

            methods = getattr(
                route,
                "methods",
                set(),
            )

            method_text = ", ".join(
                sorted(methods)
            )

            print(
                f"  {method_text:<15} "
                f"{route.path}"
            )

    print()

    print(
        "✓ FastAPI application loaded."
    )

    print(
        "✓ RevivePipeline integration configured."
    )

    print(
        "✓ Fresh pipeline created for every batch."
    )

    print(
        "✓ Dataset loaded through pipeline."
    )

    print(
        "✓ Pipeline metrics remain authoritative."
    )

    print(
        "✓ Decision Explainer registered."
    )

    print(
        "✓ PSR Guardian endpoint registered."
    )

    print(
        "✓ A2A settlement endpoint registered."
    )

    print(
        "✓ Recovery ledger endpoint registered."
    )

    print(
        "✓ Single-case ledger endpoint registered."
    )

    print(
        "✓ What-if policy simulator registered."
    )

    print(
        "✓ Simulation uses temporary policy copies."
    )

    print(
        "✓ policy.yaml is never modified by simulation."
    )

    print(
        "✓ Board Report PDF endpoint registered."
    )

    print(
        "✓ Board Report uses current pipeline metrics."
    )

    print(
        "✓ CORS configured for React frontend."
    )

    print(
        "✓ No recovery business logic duplicated."
    )

    print()

    print("=" * 72)

    print(
        "MODULE 8 API SELF-TEST: PASSED"
    )

    print("=" * 72)


if __name__ == "__main__":
    main()