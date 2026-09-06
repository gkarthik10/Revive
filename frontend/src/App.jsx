import { useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import "./App.css";
import { formatCurrency, formatDate } from "./formatters.js";
import CopilotWidget from "./CopilotWidget.jsx";
import VoiceScriptPanel from "./VoiceScriptPanel.jsx";
import Dropdown from "./Dropdown.jsx";
import UserMenu from "./UserMenu.jsx";
import ThemeToggle from "./ThemeToggle.jsx";
import KanbanBoard from "./KanbanBoard.jsx";
import ContactAdminModal from "./ContactAdminModal.jsx";
import { useAuth } from "./AuthContext.jsx";

const API_BASE = "http://127.0.0.1:8000/api";

/*
 * Named policy presets for the "Recovery Policy Performance" panel.
 *
 * These are NOT fabricated numbers — each preset is sent as-is to the
 * real /api/simulate endpoint (the same endpoint the What-If Simulator
 * uses), which runs the actual RevivePipeline against the authoritative
 * 105-case benchmark with a temporary in-memory policy override.
 * policy.yaml and the case dataset are never touched.
 *
 * Tune these two override sets if you want the panel to line up with a
 * specific demo run — whatever values are here are exactly what gets
 * sent to the engine.
 */
const POLICY_PRESETS = {
  conservative: {
    label: "Conservative",
    description: "Fewer contacts, no discount room, longer cooldowns.",
    overrides: {
      max_contact_attempts: 2,
      max_discount_percent: 5,
      max_negotiation_rounds: 2,
      cooldown_hours: 48,
      retry_max_attempts: 1,
    },
  },
  balanced: {
    label: "Balanced",
    description:
      "Slightly more contact, discount, and retry headroom than the live policy.",
    overrides: {
      max_contact_attempts: 4,
      max_discount_percent: 15,
      max_negotiation_rounds: 5,
      cooldown_hours: 18,
      retry_max_attempts: 4,
    },
  },
};

/* ============================================================
   Formatting
============================================================ */

function formatPercent(value) {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "0.00%";
  }

  return `${(number * 100).toFixed(2)}%`;
}

function formatRelativeTime(value) {
  if (!value) return "just now";

  const then = new Date(value).getTime();

  if (!Number.isFinite(then)) return "just now";

  const diffMs = Date.now() - then;
  const minutes = Math.floor(diffMs / 60000);

  if (minutes <= 0) return "just now";
  if (minutes === 1) return "1 min ago";
  if (minutes < 60) return `${minutes} min ago`;

  const hours = Math.floor(minutes / 60);

  if (hours === 1) return "1 hr ago";
  if (hours < 24) return `${hours} hrs ago`;

  const days = Math.floor(hours / 24);

  return days === 1 ? "1 day ago" : `${days} days ago`;
}

function formatLocalDateTimeInputValue(date) {
  const pad = (value) => String(value).padStart(2, "0");

  return [
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  ].join("T");
}

/* ============================================================
   Notification severity — the backend's casing/vocabulary for
   this has changed more than once already, so this normalizes
   defensively instead of assuming an exact string match. Unknown
   values fall through to a safe default class rather than
   silently rendering unstyled.
============================================================ */

function normalizeSeverity(rawSeverity) {
  const value = String(rawSeverity || "").toLowerCase();

  if (["critical", "high"].includes(value)) return value;
  if (["warning", "warn"].includes(value)) return "warning";
  if (["medium"].includes(value)) return "medium";
  if (["low"].includes(value)) return "low";
  if (["success", "ok", "recovered"].includes(value)) return "success";
  if (["info", "information", "summary"].includes(value)) return "info";

  return "default";
}

/* ============================================================
   Stop Reason
============================================================ */

function isPolicyBlocked(item) {
  return (
    item?.policy_allowed === false ||
    (Array.isArray(item?.policy_blocking_reasons) &&
      item.policy_blocking_reasons.length > 0)
  );
}

function stopReason(item) {
  const decision = String(item?.roi_decision || "").toUpperCase();

  if (decision !== "STOP" && decision !== "STOPPED") {
    return null;
  }

  if (isPolicyBlocked(item)) {
    const reasons = Array.isArray(item.policy_blocking_reasons)
      ? item.policy_blocking_reasons
      : [];

    return `Policy: ${reasons[0] || "blocked by compliance rule"}`;
  }

  return `Negative EV — P(success) ${formatPercent(
    item?.roi_probability,
  )}, would cost more than it recovers`;
}

function stopType(item) {
  const decision = String(item?.roi_decision || "").toUpperCase();

  if (decision !== "STOP" && decision !== "STOPPED") {
    return null;
  }

  return isPolicyBlocked(item) ? "POLICY BLOCKED" : "NEGATIVE EV";
}

/* ============================================================
   Status Badge
============================================================ */

function StatusBadge({ value }) {
  const normalized = String(value || "").toUpperCase();

  let className = "badge badge-neutral";

  if (
    normalized === "RECOVERED" ||
    normalized === "SETTLED" ||
    normalized === "PURSUE" ||
    normalized === "ALLOWED" ||
    normalized === "ACCEPTED" ||
    normalized === "ELIGIBLE" ||
    normalized === "PAID" ||
    normalized === "KEPT" ||
    normalized === "SCHEDULED"
  ) {
    className = "badge badge-success";
  } else if (
    normalized === "STOPPED" ||
    normalized === "STOP" ||
    normalized === "REJECTED" ||
    normalized === "PENDING" ||
    normalized === "UNRECOVERED" ||
    normalized === "NOT_RECOVERED" ||
    normalized === "PROMISED" ||
    normalized === "ESCALATE"
  ) {
    className = "badge badge-warning";
  } else if (
    normalized === "BLOCKED" ||
    normalized === "POLICY BLOCKED" ||
    normalized === "CRITICAL" ||
    normalized === "DENIED" ||
    normalized === "FAILED" ||
    normalized === "BROKEN"
  ) {
    className = "badge badge-danger";
  }

  return <span className={className}>{value || "—"}</span>;
}

/* ============================================================
   Empty State
============================================================ */

function EmptyState({ children }) {
  return <div className="empty-card">{children}</div>;
}

/* ============================================================
   A2A Helpers
============================================================ */

function getA2aOutcome(item) {
  return String(
    item?.outcome ??
      item?.a2a_outcome ??
      item?.settlement_status ??
      item?.status ??
      "",
  )
    .trim()
    .toUpperCase();
}

function getA2aEligibility(item) {
  if (item?.eligible === true) return true;
  if (item?.eligible === false) return false;

  const value = String(item?.eligibility ?? item?.eligibility_status ?? "")
    .trim()
    .toUpperCase();

  return value === "ELIGIBLE";
}

/* ============================================================
   Ledger Helpers
============================================================ */

function getLedgerAttempt(event, index, ledger) {
  if (event.roi_attempt_number !== undefined) {
    return event.roi_attempt_number;
  }

  if (event.attempt_number !== undefined) {
    return event.attempt_number;
  }

  if (event.attempt_no !== undefined) {
    return event.attempt_no;
  }

  if (event.attempt !== undefined) {
    if (typeof event.attempt === "object") {
      return (
        event.attempt.number ??
        event.attempt.attempt_number ??
        event.attempt.index ??
        1
      );
    }

    return event.attempt;
  }

  let occurrence = 0;

  for (let i = 0; i <= index; i += 1) {
    if (ledger[i]?.case_id === event.case_id) {
      occurrence += 1;
    }
  }

  return occurrence || 1;
}

function getLedgerDecision(event) {
  return (
    event.roi_decision ||
    event.decision ||
    event.status ||
    event.action_decision ||
    null
  );
}

function getLedgerOutcome(event) {
  return event.outcome || event.recovery_outcome || event.result || null;
}

function getLedgerProbability(event) {
  return (
    event.roi_probability ??
    event.p_success ??
    event.success_probability ??
    event.probability ??
    null
  );
}

function getLedgerExpectedRecovery(event) {
  return (
    event.expected_recovery ??
    event.expected_amount ??
    event.expected_value_before_cost ??
    null
  );
}

function getLedgerExpectedValue(event) {
  return event.expected_value ?? event.ev ?? null;
}

function getLedgerActionCost(event) {
  return event.action_cost ?? event.cost ?? event.recovery_cost ?? null;
}

function getLedgerRecoveredAmount(event) {
  return (
    event.recovered_amount ??
    event.recovery_amount ??
    event.amount_recovered ??
    null
  );
}

/* ============================================================
   Explanation Helpers
============================================================ */

function explanationDecisionClass(decision) {
  const normalized = String(decision || "").toUpperCase();

  if (normalized === "PURSUE") {
    return "badge badge-success";
  }

  if (normalized === "STOP" || normalized === "STOPPED") {
    return "badge badge-warning";
  }

  return "badge badge-neutral";
}

function ExplanationSection({ title, children }) {
  return (
    <div className="settlement-reason">
      <span>{title}</span>
      <p>{children}</p>
    </div>
  );
}

/* ============================================================
   Simulator Helpers
============================================================ */

function formatDifference(value, type = "currency") {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return type === "currency" ? "₹0" : "0";
  }

  if (type === "percent") {
    const sign = number > 0 ? "+" : "";

    return `${sign}${(number * 100).toFixed(2)}%`;
  }

  if (type === "integer") {
    const sign = number > 0 ? "+" : "";

    return `${sign}${number}`;
  }

  const sign = number > 0 ? "+" : number < 0 ? "−" : "";

  return `${sign}${formatCurrency(Math.abs(number))}`;
}

function differenceClass(value) {
  const number = Number(value);

  if (!Number.isFinite(number) || number === 0) {
    return "neutral-change";
  }

  return number > 0 ? "positive" : "negative";
}

/* ============================================================
   Pagination
============================================================ */

function Pagination({ page, totalPages, onChange, totalItems, pageSize }) {
  if (totalItems <= 0 || totalPages <= 1) {
    return null;
  }

  const safePage = Math.min(Math.max(page, 1), totalPages);

  return (
    <div className="pagination">
      <button
        type="button"
        className="pagination-button"
        disabled={safePage <= 1}
        onClick={() => onChange(safePage - 1)}
      >
        ← Previous
      </button>

      <div className="pagination-info">
        Page <strong>{safePage}</strong> of <strong>{totalPages}</strong>
        {pageSize ? (
          <span className="pagination-total"> · {totalItems} total</span>
        ) : null}
      </div>

      <button
        type="button"
        className="pagination-button"
        disabled={safePage >= totalPages}
        onClick={() => onChange(safePage + 1)}
      >
        Next →
      </button>
    </div>
  );
}

/* ============================================================
   Batch History Chart (inline SVG line chart, no external libs)
============================================================ */

function BatchHistoryChart({ batches }) {
  if (!Array.isArray(batches) || batches.length === 0) {
    return null;
  }

  const width = 640;
  const height = 260;
  const padding = { top: 16, right: 16, bottom: 30, left: 40 };

  const rates = batches.map((b) => Number(b.recovery_rate || 0) * 100);
  const minRate = Math.min(...rates);
  const maxRate = Math.max(...rates);

  const rangeLow = Math.floor(minRate / 5) * 5 - 5;
  const rangeHigh = Math.ceil(maxRate / 5) * 5 + 5;
  const range = Math.max(1, rangeHigh - rangeLow);

  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  function xFor(index) {
    if (batches.length === 1) return padding.left + plotWidth / 2;

    return padding.left + (index / (batches.length - 1)) * plotWidth;
  }

  function yFor(rate) {
    return padding.top + plotHeight - ((rate - rangeLow) / range) * plotHeight;
  }

  const points = batches.map((b, i) => ({
    x: xFor(i),
    y: yFor(Number(b.recovery_rate || 0) * 100),
    label: b.label,
  }));

  const linePath = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`)
    .join(" ");

  const gridLines = [rangeHigh, (rangeHigh + rangeLow) / 2, rangeLow];

  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet">
      {gridLines.map((value) => (
        <g key={value}>
          <line
            x1={padding.left}
            x2={width - padding.right}
            y1={yFor(value)}
            y2={yFor(value)}
            stroke="var(--border-soft)"
            strokeDasharray="3 4"
          />
          <text
            x={padding.left - 8}
            y={yFor(value) + 4}
            textAnchor="end"
            fontSize="10"
            fill="var(--text-faint)"
          >
            {value.toFixed(0)}%
          </text>
        </g>
      ))}

      <path d={linePath} fill="none" stroke="var(--text)" strokeWidth="1.6" />

      {points.map((p, i) => (
        <circle
          key={p.label || i}
          cx={p.x}
          cy={p.y}
          r={i === points.length - 1 ? 4.5 : 3}
          fill={i === points.length - 1 ? "var(--accent)" : "var(--text)"}
        />
      ))}

      {points.map((p, i) => (
        <text
          key={`label-${p.label || i}`}
          x={p.x}
          y={height - 8}
          textAnchor="middle"
          fontSize="10"
          fill="var(--text-faint)"
        >
          {p.label}
        </text>
      ))}
    </svg>
  );
}

/* ============================================================
   Recovery Policy Performance (real engine results, not a
   fabricated trend — see POLICY_PRESETS + loadPolicyComparison)
============================================================ */

function PolicyPerformanceBars({ items }) {
  if (!Array.isArray(items) || items.length === 0) {
    return null;
  }

  const maxRate = Math.max(...items.map((item) => item.rate || 0), 0.0001);

  return (
    <div className="policy-performance-bars">
      {items.map((item) => {
        const widthPercent = Math.max(
          4,
          Math.min(100, ((item.rate || 0) / maxRate) * 100),
        );

        return (
          <div
            className={`policy-performance-row${
              item.recommended ? " recommended" : ""
            }`}
            key={item.key}
          >
            <div className="policy-performance-row-label">
              <span className="policy-performance-name">{item.label}</span>

              {item.recommended && (
                <span className="policy-performance-badge">★ RECOMMENDED</span>
              )}
            </div>

            <div className="policy-performance-track">
              <div
                className="policy-performance-fill"
                style={{ width: `${widthPercent}%` }}
              />
            </div>

            <div className="policy-performance-value">
              {formatPercent(item.rate)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ============================================================
   Launch / dashboard micro-interactions
============================================================ */

function getTimeGreeting() {
  const hour = new Date().getHours();

  if (hour < 5) return "Good night";
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  if (hour < 21) return "Good evening";

  return "Good night";
}

function normalizePulseEvent(item, type, fallbackIndex = 0) {
  if (!item || typeof item !== "object") return null;

  const caseId =
    item.case_id || item.revive_case_tag || item.promise_case_id || null;

  const timestamp =
    item.timestamp ||
    item.created_at ||
    item.updated_at ||
    item.captured_at ||
    item.promise_date ||
    null;

  let title = "";
  let meta = "";
  let amount = Number(
    item.amount ?? item.promised_amount ?? item.recovered_amount,
  );

  if (!Number.isFinite(amount)) amount = null;

  if (type === "payment") {
    const status = String(
      item.status || item.payment_status || item.outcome || "PAYMENT",
    ).toUpperCase();

    title =
      status === "RECOVERED" || status === "CAPTURED"
        ? "Payment recovered"
        : status === "FAILED"
          ? "Payment failed"
          : "Payment activity";

    meta = caseId || "Live payment";
  } else if (type === "promise") {
    const status = String(item.status || "PROMISED").toUpperCase();

    title =
      status === "BROKEN"
        ? "Promise deadline passed"
        : status === "PAID"
          ? "Promise payment verified"
          : "Promise created";

    meta = caseId || item.promise_id || "Promise tracker";
  } else if (type === "a2a") {
    const status = String(
      item.settlement_status || item.payment_status || "A2A",
    ).toUpperCase();

    title =
      status === "AGREED"
        ? "A2A agreement reached"
        : item.recovery_confirmed
          ? "A2A recovery confirmed"
          : "A2A settlement activity";

    meta = caseId || "Live settlement";
  } else {
    title =
      item.event_type ||
      item.notification_type ||
      item.title ||
      "Recovery activity";

    meta = caseId || item.message || "Recovery ledger";
  }

  return {
    id: `${type}-${caseId || fallbackIndex}-${timestamp || fallbackIndex}`,
    type,
    caseId,
    timestamp,
    title,
    meta,
    amount,
    source: item,
  };
}

/* ============================================================
   App
============================================================ */

function App() {
  const { user } = useAuth();
  const [dashboard, setDashboard] = useState(null);
  const [cases, setCases] = useState([]);
  const [a2aSettlements, setA2aSettlements] = useState([]);
  const [liveA2aSettlements, setLiveA2aSettlements] = useState([]);
  const [a2aActionCaseId, setA2aActionCaseId] = useState(null);
  const [liveA2aError, setLiveA2aError] = useState("");
  const [selectedLiveA2a, setSelectedLiveA2a] = useState(null);
  const [ledger, setLedger] = useState([]);
  const [realCapture, setRealCapture] = useState(null);

  const [selectedCase, setSelectedCase] = useState(null);
  const [selectedSettlement, setSelectedSettlement] = useState(null);
  const [selectedLivePayment, setSelectedLivePayment] = useState(null);

  /* Decision Explainer */

  const [explanation, setExplanation] = useState(null);
  const [explainingCase, setExplainingCase] = useState(false);
  const [explanationQuestion, setExplanationQuestion] = useState("");

  /* General */

  const [loading, setLoading] = useState(true);
  const [loadingAmount, setLoadingAmount] = useState(0);
  const [runningBatch, setRunningBatch] = useState(false);
  const [exportingReport, setExportingReport] = useState(false);
  const [error, setError] = useState("");
  const [batchHistory, setBatchHistory] = useState([]);
  const [toast, setToast] = useState("");
  const toastTimerRef = useRef(null);

  /* Contact admin */
  const [showContactAdmin, setShowContactAdmin] = useState(false);

  /* Recovery Pulse */
  const [pulseFilter, setPulseFilter] = useState("ALL");

  function showToast(message) {
    setToast(message);

    if (toastTimerRef.current) {
      window.clearTimeout(toastTimerRef.current);
    }

    toastTimerRef.current = window.setTimeout(() => {
      setToast("");
    }, 3200);
  }

  /* Case filters */

  const [search, setSearch] = useState("");
  const [decisionFilter, setDecisionFilter] = useState("ALL");
  const [outcomeFilter, setOutcomeFilter] = useState("ALL");

  /* A2A filters */

  const [a2aFilter, setA2aFilter] = useState("ALL");

  /* Pagination — client-side, resets to page 1 whenever the
     underlying filtered set changes so a stale page number never
     shows an empty table. */

  const PAGE_SIZE = 12;
  const [casesPage, setCasesPage] = useState(1);
  const [ledgerPage, setLedgerPage] = useState(1);

  /* Live Razorpay payments */

  const [livePayments, setLivePayments] = useState([]);
  const [liveMetrics, setLiveMetrics] = useState(null);
  const [checkoutForm, setCheckoutForm] = useState({
    amount: "",
    customer_name: "",
    customer_email: "",
    customer_id: "",
    surface: "subscription_failure",
    invoice_id: "",
    has_ap_agent: false,
    disputed: false,
  });
  const [knownCustomers, setKnownCustomers] = useState([]);
  const [creatingCheckout, setCreatingCheckout] = useState(false);
  const [checkoutError, setCheckoutError] = useState("");
  const [resettingLiveCases, setResettingLiveCases] = useState(false);
  const [retryingCaseId, setRetryingCaseId] = useState(null);
  const [retryError, setRetryError] = useState("");

  /* Notifications */

  const [notificationsList, setNotificationsList] = useState([]);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const notificationRef = useRef(null);

  /* Ops Task Board (Kanban) — now opened from a topbar icon button
     instead of living inline in the dashboard/sidebar. */

  const [kanbanOpen, setKanbanOpen] = useState(false);
  const [kanbanPulse, setKanbanPulse] = useState(null);
  const [kanbanPulseVisible, setKanbanPulseVisible] = useState(false);
  const kanbanPulseHideTimerRef = useRef(null);

  /* Refs mirror the latest live data so the recurring pulse timer
     can read current counts without resetting itself every time the
     5-second polling effect updates state. */

  const livePaymentsRef = useRef([]);
  const promisesRef = useRef([]);
  const liveA2aSettlementsRef = useRef([]);
  const psrAlertsRef = useRef([]);

  /* Promise-to-Pay Tracker */

  const [promises, setPromises] = useState([]);
  const [promiseMetrics, setPromiseMetrics] = useState(null);
  const [promiseForm, setPromiseForm] = useState({
    case_id: "",
    promised_amount: "",
    promise_date: "",
    customer_email: "",
  });
  const [creatingPromise, setCreatingPromise] = useState(false);
  const [promiseError, setPromiseError] = useState("");
  const [promiseActionId, setPromiseActionId] = useState("");
  const [promiseHistory, setPromiseHistory] = useState(null);
  const [promiseHistoryLoading, setPromiseHistoryLoading] = useState(false);

  /* ==========================================================
     WHAT-IF SIMULATOR
  ========================================================== */

  const [simulationOverrides, setSimulationOverrides] = useState({
    max_contact_attempts: 3,
    max_discount_percent: 10,
    max_negotiation_rounds: 4,
    cooldown_hours: 24,
    retry_max_attempts: 3,
  });

  const [simulationResult, setSimulationResult] = useState(null);
  const [runningSimulation, setRunningSimulation] = useState(false);
  const [simulationError, setSimulationError] = useState("");
  // Bumped on every completed run so the result card remounts and its
  // reveal animation replays smoothly instead of the numbers popping in.
  const [simulationRunId, setSimulationRunId] = useState(0);

  /* ==========================================================
     RECOVERY POLICY PERFORMANCE
     (Conservative / Current / Balanced — real /api/simulate calls)
  ========================================================== */

  const [policyComparison, setPolicyComparison] = useState(null);
  const [policyComparisonLoading, setPolicyComparisonLoading] = useState(false);
  const [policyComparisonError, setPolicyComparisonError] = useState("");

  async function loadPolicyComparison() {
    try {
      setPolicyComparisonLoading(true);
      setPolicyComparisonError("");

      const [conservativeResponse, balancedResponse] = await Promise.all([
        axios.post(
          `${API_BASE}/simulate`,
          POLICY_PRESETS.conservative.overrides,
        ),
        axios.post(`${API_BASE}/simulate`, POLICY_PRESETS.balanced.overrides),
      ]);

      setPolicyComparison({
        conservative: conservativeResponse.data?.simulation || null,
        balanced: balancedResponse.data?.simulation || null,
      });
    } catch (err) {
      console.error("Policy comparison error:", err);

      const backendMessage =
        err?.response?.data?.detail ||
        "Could not generate the policy performance comparison.";

      setPolicyComparisonError(
        typeof backendMessage === "string"
          ? backendMessage
          : JSON.stringify(backendMessage),
      );
    } finally {
      setPolicyComparisonLoading(false);
    }
  }

  /* ==========================================================
     Load Dashboard
  ========================================================== */

  async function loadDashboard(showLoading = true) {
    const launchStartedAt = performance.now();

    try {
      if (showLoading) {
        setLoading(true);
      }

      setError("");

      const [
        dashboardResponse,
        casesResponse,
        a2aResponse,
        liveA2aResponse,
        ledgerResponse,
        realCaptureResponse,
      ] = await Promise.all([
        axios.get(`${API_BASE}/dashboard`),
        axios.get(`${API_BASE}/cases`),
        axios.get(`${API_BASE}/a2a`),
        axios.get(`${API_BASE}/a2a/live-settlements`).catch(() => null),
        axios.get(`${API_BASE}/ledger`),
        axios.get(`${API_BASE}/real-capture`).catch(() => null),
      ]);

      setDashboard(dashboardResponse.data || {});

      setCases(
        Array.isArray(casesResponse.data?.cases)
          ? casesResponse.data.cases
          : [],
      );

      setA2aSettlements(
        Array.isArray(a2aResponse.data?.settlements)
          ? a2aResponse.data.settlements
          : [],
      );

      setLiveA2aSettlements(
        Array.isArray(liveA2aResponse?.data?.settlements)
          ? liveA2aResponse.data.settlements
          : [],
      );

      setLedger(
        Array.isArray(ledgerResponse.data?.events)
          ? ledgerResponse.data.events
          : [],
      );

      setRealCapture(
        realCaptureResponse?.data?.found ? realCaptureResponse.data.case : null,
      );
    } catch (err) {
      console.error("Revive API error:", err);

      setError(
        "Unable to connect to the Revive backend. Make sure FastAPI is running on port 8000.",
      );
    } finally {
      if (showLoading) {
        const elapsed = performance.now() - launchStartedAt;
        const minimumLaunchTime = 2400;
        const counterAnimationDuration = 1800;

        // Wait for whichever is longer: the minimum splash time
        // measured from fetch start, or the full counter animation
        // measured from when the data actually arrived. This
        // guarantees the recovered-revenue counter always finishes
        // counting up before the loading screen unmounts, even on
        // a slow network where the fetch itself eats into the
        // minimum launch window.
        const remaining = Math.max(
          minimumLaunchTime - elapsed,
          counterAnimationDuration,
        );

        window.setTimeout(() => {
          setLoading(false);
        }, remaining);
      } else {
        setLoading(false);
      }
    }
  }

  /* ==========================================================
     Smooth launch amount animation
  ========================================================== */

  useEffect(() => {
    if (!loading || !dashboard) return undefined;

    const target = Number(dashboard?.summary?.recovered_revenue);

    if (!Number.isFinite(target) || target <= 0) {
      setLoadingAmount(0);
      return undefined;
    }

    const startedAt = performance.now();
    const duration = 1800;
    let frameId = 0;

    const tick = (now) => {
      const progress = Math.min(1, (now - startedAt) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);

      setLoadingAmount(Math.round(target * eased));

      if (progress < 1) {
        frameId = window.requestAnimationFrame(tick);
      }
    };

    frameId = window.requestAnimationFrame(tick);

    return () => window.cancelAnimationFrame(frameId);
  }, [loading, dashboard]);

  /* ==========================================================
     Batch History
  ========================================================== */

  async function loadBatchHistory() {
    try {
      const response = await axios.get(`${API_BASE}/batch-history`);

      setBatchHistory(
        Array.isArray(response.data?.batches) ? response.data.batches : [],
      );
    } catch (err) {
      console.error("Batch history fetch error:", err);
    }
  }

  /* ==========================================================
     Run Batch
  ========================================================== */

  async function runRecoveryBatch() {
    try {
      setRunningBatch(true);
      setError("");

      setExplanation(null);
      setSelectedCase(null);

      await axios.post(`${API_BASE}/run-batch`);

      await loadDashboard(false);
      await loadNotifications();
      await loadBatchHistory();

      showToast("Recovery batch completed");
    } catch (err) {
      console.error("Recovery batch error:", err);

      setError("Recovery batch could not be completed.");
    } finally {
      setRunningBatch(false);
    }
  }

  /* ==========================================================
     Export Board Report
  ========================================================== */

  async function exportBoardReport() {
    try {
      setExportingReport(true);
      setError("");

      const response = await axios.get(`${API_BASE}/board-report`, {
        responseType: "blob",
      });

      const contentType = response.headers["content-type"] || "";

      if (!contentType.includes("application/pdf")) {
        throw new Error("The backend did not return a PDF.");
      }

      const blob = new Blob([response.data], {
        type: "application/pdf",
      });

      const url = window.URL.createObjectURL(blob);

      const link = document.createElement("a");

      link.href = url;
      link.download = "Revive_Board_Report.pdf";

      document.body.appendChild(link);
      link.click();
      link.remove();

      window.setTimeout(() => {
        window.URL.revokeObjectURL(url);
      }, 1000);

      showToast(
        `${filteredCases.length} filtered cases exported with evidence`,
      );
    } catch (err) {
      console.error("Board report export error:", err);

      setError(
        "Board Report could not be generated. Check that the FastAPI /api/board-report endpoint is running.",
      );
    } finally {
      setExportingReport(false);
    }
  }

  /* ==========================================================
     Explain Case
  ========================================================== */

  async function explainCase(caseId, question = null) {
    try {
      setExplainingCase(true);
      setError("");

      const cleanQuestion =
        question && String(question).trim() ? String(question).trim() : null;

      const response = await axios.post(
        `${API_BASE}/cases/${encodeURIComponent(caseId)}/explain`,
        cleanQuestion
          ? {
              question: cleanQuestion,
            }
          : {},
      );

      setExplanation(response.data || null);
    } catch (err) {
      console.error("Decision explanation error:", err);

      const backendMessage =
        err?.response?.data?.detail ||
        "Unable to generate the decision explanation.";

      setError(String(backendMessage));
    } finally {
      setExplainingCase(false);
    }
  }

  function openCase(item) {
    setSelectedCase(item);
    setExplanation(null);
    setExplanationQuestion("");
  }

  function closeCaseModal() {
    setSelectedCase(null);
    setExplanation(null);
    setExplanationQuestion("");
  }

  function openLivePayment(item) {
    setSelectedLivePayment(item);
  }

  function closeLivePaymentModal() {
    setSelectedLivePayment(null);
  }

  function closeExplanationModal() {
    setExplanation(null);
    setExplanationQuestion("");
  }

  async function handleExplainSelectedCase() {
    if (!selectedCase?.case_id) {
      return;
    }

    await explainCase(selectedCase.case_id, explanationQuestion);
  }

  /* ==========================================================
     WHAT-IF POLICY SIMULATOR
  ========================================================== */

  function updateSimulationOverride(name, value) {
    setSimulationOverrides((current) => ({
      ...current,
      [name]: value,
    }));
  }

  function resetSimulation() {
    setSimulationOverrides({
      max_contact_attempts: 3,
      max_discount_percent: 10,
      max_negotiation_rounds: 4,
      cooldown_hours: 24,
      retry_max_attempts: 3,
    });

    setSimulationResult(null);
    setSimulationError("");
  }

  async function runPolicySimulation() {
    try {
      setRunningSimulation(true);
      setSimulationError("");
      setError("");

      const payload = {
        max_contact_attempts: Number(simulationOverrides.max_contact_attempts),

        max_discount_percent: Number(simulationOverrides.max_discount_percent),

        max_negotiation_rounds: Number(
          simulationOverrides.max_negotiation_rounds,
        ),

        cooldown_hours: Number(simulationOverrides.cooldown_hours),

        retry_max_attempts: Number(simulationOverrides.retry_max_attempts),
      };

      // The pipeline usually answers in well under a second. Pairing the
      // request with a small minimum-visible-time keeps the "Simulating..."
      // state from flickering for a single frame and lets the result card
      // ease in smoothly instead of snapping into place instantly.
      const MIN_VISIBLE_MS = 550;
      const startedAt = Date.now();

      const response = await axios.post(`${API_BASE}/simulate`, payload);

      const elapsed = Date.now() - startedAt;
      if (elapsed < MIN_VISIBLE_MS) {
        await new Promise((resolve) =>
          setTimeout(resolve, MIN_VISIBLE_MS - elapsed),
        );
      }

      setSimulationResult(response.data || null);
      setSimulationRunId((value) => value + 1);

      showToast("What-if simulation ready");
    } catch (err) {
      console.error("Policy simulation error:", err);

      const backendMessage =
        err?.response?.data?.detail ||
        "Policy simulation could not be completed.";

      setSimulationError(
        typeof backendMessage === "string"
          ? backendMessage
          : JSON.stringify(backendMessage),
      );
    } finally {
      setRunningSimulation(false);
    }
  }

  /* ==========================================================
     Live Razorpay Payments
  ========================================================== */

  async function loadLiveA2aSettlements() {
    try {
      const response = await axios.get(`${API_BASE}/a2a/live-settlements`);

      setLiveA2aSettlements(
        Array.isArray(response.data?.settlements)
          ? response.data.settlements
          : [],
      );
    } catch (err) {
      console.error("Live A2A settlements fetch error:", err);
    }
  }

  function isLiveA2aEligible(item) {
    return (
      String(item?.surface || "").toLowerCase() === "b2b_receivable" &&
      item?.has_ap_agent === true &&
      item?.disputed !== true &&
      Boolean(item?.invoice_id) &&
      String(item?.recovery_status || "").toUpperCase() === "PENDING_RECOVERY"
    );
  }

  function getLiveA2aForCase(caseId) {
    return liveA2aSettlements.find((item) => item.case_id === caseId) || null;
  }

  function getLiveA2aStage(settlement) {
    if (!settlement) return "READY";
    if (settlement.recovery_confirmed === true) return "CONFIRMED";
    if (String(settlement.payment_status || "").toUpperCase() === "PENDING") {
      return settlement.payment_link_id ? "PAYMENT PENDING" : "AGREED";
    }
    return String(
      settlement.payment_status || settlement.settlement_status || "AGREED",
    ).toUpperCase();
  }

  async function startLiveA2aSettlement(caseItem) {
    const caseId = caseItem?.case_id;

    if (!caseId) return;

    try {
      setA2aActionCaseId(caseId);
      setLiveA2aError("");

      const response = await axios.post(
        `${API_BASE}/a2a/live/${encodeURIComponent(caseId)}/settle`,
      );

      const agreement = response.data?.agreement || null;
      const paymentUrl =
        response.data?.payment?.short_url || agreement?.payment_url || null;

      await Promise.all([loadLiveA2aSettlements(), loadLivePayments()]);

      if (paymentUrl) {
        window.open(paymentUrl, "_blank", "noopener,noreferrer");
        showToast(
          response.data?.resumed
            ? "A2A agreement resumed — payment link ready"
            : "A2A agreement reached — payment link ready",
        );
      } else if (agreement) {
        setSelectedLiveA2a(agreement);
        showToast(
          response.data?.resumed
            ? "A2A agreement resumed"
            : "A2A agreement reached",
        );
      }
    } catch (err) {
      console.error("Live A2A settlement error:", err);

      const detail = err?.response?.data?.detail;
      setLiveA2aError(
        typeof detail === "string"
          ? detail
          : detail?.message ||
              "Live A2A settlement could not be started. The agreement, if already created, remains safely preserved.",
      );

      await loadLiveA2aSettlements();
    } finally {
      setA2aActionCaseId(null);
    }
  }

  async function loadLivePayments() {
    try {
      const response = await axios.get(`${API_BASE}/payments/live-cases`);

      setLivePayments(
        Array.isArray(response.data?.cases) ? response.data.cases : [],
      );
    } catch (err) {
      console.error("Live payments fetch error:", err);
    }
  }

  async function loadLiveMetrics() {
    try {
      const response = await axios.get(`${API_BASE}/metrics`);

      setLiveMetrics(response.data?.live_metrics || null);
    } catch (err) {
      console.error("Live metrics fetch error:", err);
    }
  }

  async function loadCustomers() {
    try {
      const response = await axios.get(`${API_BASE}/customers`);

      setKnownCustomers(
        Array.isArray(response.data?.customers) ? response.data.customers : [],
      );
    } catch (err) {
      console.error("Customer directory fetch error:", err);
    }
  }

  async function createLiveCheckout() {
    const amount = Number(checkoutForm.amount);

    if (!Number.isFinite(amount) || amount <= 0) {
      setCheckoutError("Enter a valid amount.");
      return;
    }

    try {
      setCreatingCheckout(true);
      setCheckoutError("");

      const response = await axios.post(`${API_BASE}/payments/checkout`, {
        amount,
        customer_name: checkoutForm.customer_name || "Revive Customer",
        customer_email: checkoutForm.customer_email.trim() || null,
        customer_id: checkoutForm.customer_id.trim() || null,
        surface: checkoutForm.surface,
        invoice_id: checkoutForm.invoice_id.trim() || null,
        has_ap_agent: checkoutForm.has_ap_agent,
        disputed: checkoutForm.disputed,
      });

      const shortUrl = response.data?.short_url;

      if (shortUrl) {
        window.open(shortUrl, "_blank", "noopener,noreferrer");
      } else {
        setCheckoutError("Razorpay did not return a checkout link.");
      }
    } catch (err) {
      console.error("Checkout creation error:", err);

      setCheckoutError(
        err.response?.data?.detail ||
          "Could not create a Razorpay checkout link. Make sure " +
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are set on the backend.",
      );
    } finally {
      setCreatingCheckout(false);
    }
  }

  async function retryLivePayment(caseId) {
    try {
      setRetryError("");
      setRetryingCaseId(caseId);

      const response = await axios.post(
        `${API_BASE}/payments/live-cases/${encodeURIComponent(caseId)}/retry`,
      );

      const shortUrl = response.data?.short_url;

      if (shortUrl) {
        window.open(shortUrl, "_blank", "noopener,noreferrer");
      } else {
        setRetryError("Razorpay did not return a checkout link.");
      }

      // A retry only issues a new payment link — it never marks the
      // case recovered. Refresh so retry_count is reflected, but the
      // case stays PENDING_RECOVERY until a real payment.captured
      // webhook lands.
      await loadLivePayments();
    } catch (err) {
      console.error("Retry payment error:", err);

      setRetryError(
        err.response?.data?.detail ||
          "Could not create a retry payment link for this case.",
      );
    } finally {
      setRetryingCaseId(null);
    }
  }

  async function resetLivePayments() {
    try {
      setResettingLiveCases(true);

      await axios.delete(`${API_BASE}/payments/live-cases`);

      await loadLivePayments();
      await loadDashboard();
      await loadNotifications();
    } catch (err) {
      console.error("Reset live payments error:", err);
    } finally {
      setResettingLiveCases(false);
    }
  }

  /* ==========================================================
     Notifications
  ========================================================== */

  async function loadNotifications() {
    try {
      const response = await axios.get(`${API_BASE}/notifications`);

      setNotificationsList(
        Array.isArray(response.data?.notifications)
          ? response.data.notifications
          : [],
      );
    } catch (err) {
      console.error("Notifications fetch error:", err);
    }
  }

  /* ==========================================================
     Promise-to-Pay Tracker
  ========================================================== */

  async function loadPromises() {
    try {
      const response = await axios.get(`${API_BASE}/promises`);

      setPromises(
        Array.isArray(response.data?.promises) ? response.data.promises : [],
      );
      setPromiseMetrics(response.data?.metrics || null);
    } catch (err) {
      console.error("Promises fetch error:", err);
    }
  }

  const promiseCaseOptions = useMemo(() => {
    const merged = [...(Array.isArray(cases) ? cases : [])];
    const existing = new Set(
      merged.map((item) =>
        String(item?.case_id || "")
          .trim()
          .toLowerCase(),
      ),
    );

    for (const liveCase of Array.isArray(livePayments) ? livePayments : []) {
      const id = String(liveCase?.case_id || "").trim();
      const key = id.toLowerCase();
      if (id && !existing.has(key)) {
        merged.push({ ...liveCase, _live_payment_case: true });
        existing.add(key);
      }
    }

    return merged;
  }, [cases, livePayments]);

  function getPromiseCase(caseId) {
    return (
      promiseCaseOptions.find(
        (item) =>
          String(item?.case_id || "")
            .trim()
            .toLowerCase() ===
          String(caseId || "")
            .trim()
            .toLowerCase(),
      ) || null
    );
  }

  const promiseCasePreview = useMemo(
    () => getPromiseCase(promiseForm.case_id),
    [promiseCaseOptions, promiseForm.case_id],
  );

  function getPromiseOutstandingAmount(caseItem) {
    if (!caseItem) return null;

    const direct =
      caseItem.outstanding_amount ??
      caseItem.outstanding ??
      caseItem.remaining_amount;

    if (Number.isFinite(Number(direct))) {
      return Number(direct);
    }

    const amount = Number(caseItem.amount);
    const recovered = Number(
      caseItem.recovered_amount ?? caseItem.recovery_amount ?? 0,
    );

    if (Number.isFinite(amount)) {
      return Math.max(0, amount - (Number.isFinite(recovered) ? recovered : 0));
    }

    return null;
  }

  function updatePromiseFormField(field, value) {
    setPromiseError("");

    setPromiseForm((prev) => ({
      ...prev,
      [field]: value,
    }));
  }

  function handlePromiseCaseChange(value) {
    const caseItem = getPromiseCase(value);

    setPromiseError("");

    setPromiseForm((prev) => ({
      ...prev,
      case_id: value,
      promised_amount:
        caseItem && !prev.promised_amount
          ? String(getPromiseOutstandingAmount(caseItem) ?? "")
          : prev.promised_amount,
    }));
  }

  async function createPromise() {
    setPromiseError("");

    const caseId = promiseForm.case_id.trim();

    if (!caseId) {
      setPromiseError("Select or enter a case ID.");
      return;
    }

    const caseItem = getPromiseCase(caseId);

    if (!caseItem) {
      setPromiseError(
        "That case ID does not exist in the current Recovery Cases dataset.",
      );
      return;
    }

    const amount = Number(promiseForm.promised_amount);

    if (!Number.isFinite(amount) || amount <= 0) {
      setPromiseError("Enter a valid promised amount.");
      return;
    }

    const outstandingAmount = getPromiseOutstandingAmount(caseItem);

    if (
      Number.isFinite(outstandingAmount) &&
      outstandingAmount > 0 &&
      amount > outstandingAmount
    ) {
      setPromiseError(
        `Promised amount cannot exceed the outstanding amount of ${formatCurrency(
          outstandingAmount,
        )}.`,
      );
      return;
    }

    if (!promiseForm.promise_date) {
      setPromiseError("Choose a promise date and time.");
      return;
    }

    const promiseDate = new Date(promiseForm.promise_date);

    if (Number.isNaN(promiseDate.getTime())) {
      setPromiseError("Enter a valid promise date and time.");
      return;
    }

    if (promiseDate.getTime() <= Date.now()) {
      setPromiseError("Promise date and time must be in the future.");
      return;
    }

    try {
      setCreatingPromise(true);

      const response = await axios.post(`${API_BASE}/promises`, {
        case_id: caseId,
        customer_id: caseItem.customer_id ?? null,
        customer_name: caseItem.customer_name ?? caseItem.customer ?? null,
        invoice_id: caseItem.invoice_id ?? null,
        customer_email: promiseForm.customer_email.trim() || null,
        promised_amount: amount,
        outstanding_amount: outstandingAmount,
        // datetime-local is intentionally sent without UTC conversion.
        // The selected value is a local wall-clock commitment (IST for this
        // deployment), so toISOString() would shift it by the timezone offset.
        promise_date: promiseForm.promise_date,
      });

      setPromiseForm({
        case_id: "",
        promised_amount: "",
        promise_date: "",
        customer_email: "",
      });

      await loadPromises();
      await loadNotifications();

      const paymentUrl = response.data?.payment_link?.short_url;
      if (paymentUrl) {
        window.open(paymentUrl, "_blank", "noopener,noreferrer");
        showToast("Promise recorded — Razorpay payment link opened");
      } else {
        showToast(
          "Promise recorded — payment link could not be created yet; use Retry on the promise row.",
        );
      }
    } catch (err) {
      console.error("Create promise error:", err);

      setPromiseError(
        err.response?.data?.detail || "Could not record the promise.",
      );
    } finally {
      setCreatingPromise(false);
    }
  }

  async function createPromisePaymentLink(caseId) {
    try {
      setPromiseActionId(caseId);
      const response = await axios.post(
        `${API_BASE}/promises/${encodeURIComponent(caseId)}/payment-link`,
      );
      await loadPromises();
      const url = response.data?.payment_link?.short_url;
      if (url) {
        window.open(url, "_blank", "noopener,noreferrer");
        showToast(
          response.data?.idempotent
            ? "Existing Razorpay payment link opened"
            : "Razorpay payment link created and opened",
        );
      }
    } catch (err) {
      console.error("Create promise payment link error:", err);
      setPromiseError(
        err.response?.data?.detail ||
          "Could not create the Razorpay payment link.",
      );
    } finally {
      setPromiseActionId("");
    }
  }

  async function resolvePromise(caseId, outcome) {
    const isPaid = outcome === "mark-paid";

    const confirmed = window.confirm(
      isPaid
        ? "Mark this promise as fulfilled?\n\nManual fulfillment is not authoritative payment proof. A Razorpay webhook/payment capture is still required for verified payment evidence."
        : "Mark this promise as broken?\n\nAutomated recovery can become eligible again after this action.",
    );

    if (!confirmed) return;

    try {
      setPromiseActionId(caseId);

      const response = await axios.post(
        `${API_BASE}/promises/${encodeURIComponent(caseId)}/${outcome}`,
      );

      await loadPromises();
      await loadDashboard(false);
      await loadNotifications();

      if (isPaid && response.data?.promise?.payment_verified !== true) {
        showToast("Promise fulfilled manually — payment remains unverified");
      } else {
        showToast(
          isPaid
            ? "Promise fulfilled with verified payment evidence"
            : "Promise marked broken",
        );
      }
    } catch (err) {
      console.error("Resolve promise error:", err);

      setPromiseError(
        err.response?.data?.detail || "Could not update the promise.",
      );
    } finally {
      setPromiseActionId("");
    }
  }

  async function openPromiseHistory(caseId) {
    try {
      setPromiseHistoryLoading(true);
      setPromiseHistory(null);

      const response = await axios.get(
        `${API_BASE}/promises/${encodeURIComponent(caseId)}/history`,
      );

      setPromiseHistory(response.data || null);
    } catch (err) {
      console.error("Promise history fetch error:", err);

      setPromiseError(
        err.response?.data?.detail || "Could not load promise history.",
      );
    } finally {
      setPromiseHistoryLoading(false);
    }
  }

  function closePromiseHistory() {
    setPromiseHistory(null);
    setPromiseHistoryLoading(false);
  }

  /* ==========================================================
     Initial Load
  ========================================================== */

  useEffect(() => {
    loadDashboard();
    loadLivePayments();
    loadLiveA2aSettlements();
    loadLiveMetrics();
    loadNotifications();
    loadPromises();
    loadBatchHistory();
    loadCustomers();
    loadPolicyComparison();

    const interval = setInterval(() => {
      loadLivePayments();
      loadLiveA2aSettlements();
      loadLiveMetrics();
      loadNotifications();
      loadPromises();
      loadCustomers();
    }, 5000);

    return () => clearInterval(interval);
  }, []);

  /* ==========================================================
     Close notification panel on outside click
  ========================================================== */

  useEffect(() => {
    if (!notificationsOpen) {
      return undefined;
    }

    function handleClickOutside(event) {
      if (
        notificationRef.current &&
        !notificationRef.current.contains(event.target)
      ) {
        setNotificationsOpen(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside);

    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [notificationsOpen]);

  /* Computed early (well before the loading early-return further
     down) so it's available as a stable dependency for the effects
     below, and so it can be reused for the section render later
     without a second, duplicate derivation. */

  const psrAlerts = useMemo(
    () => (Array.isArray(dashboard?.psr_alerts) ? dashboard.psr_alerts : []),
    [dashboard],
  );

  useEffect(() => {
    if (!kanbanOpen) {
      return undefined;
    }

    function handleEscape(event) {
      if (event.key === "Escape") {
        setKanbanOpen(false);
      }
    }

    document.addEventListener("keydown", handleEscape);

    return () => document.removeEventListener("keydown", handleEscape);
  }, [kanbanOpen]);

  /* ==========================================================
     Ops Task Board — keep refs fresh so the recurring pulse timer
     below can read current counts without needing to be recreated.
  ========================================================== */

  useEffect(() => {
    livePaymentsRef.current = livePayments;
  }, [livePayments]);

  useEffect(() => {
    promisesRef.current = promises;
  }, [promises]);

  useEffect(() => {
    liveA2aSettlementsRef.current = liveA2aSettlements;
  }, [liveA2aSettlements]);

  useEffect(() => {
    psrAlertsRef.current = psrAlerts;
  }, [psrAlerts]);

  /* ==========================================================
     Ops Task Board — recurring "coming and going" pulse message.
     Fires every 30 minutes, stays visible for a few seconds, then
     fades back out — a lightweight nudge toward the board rather
     than a persistent badge.
  ========================================================== */

  useEffect(() => {
    function buildKanbanPulse() {
      const pendingPayments = (
        Array.isArray(livePaymentsRef.current) ? livePaymentsRef.current : []
      ).filter(
        (item) =>
          String(item.recovery_status || item.outcome || "").toUpperCase() ===
          "PENDING_RECOVERY",
      ).length;

      const activePromises = (
        Array.isArray(promisesRef.current) ? promisesRef.current : []
      ).filter(
        (item) => String(item.status || "").toUpperCase() === "PROMISED",
      ).length;

      const pendingA2a = (
        Array.isArray(liveA2aSettlementsRef.current)
          ? liveA2aSettlementsRef.current
          : []
      ).filter(
        (item) =>
          !item.recovery_confirmed &&
          String(item.payment_status || "").toUpperCase() === "PENDING",
      ).length;

      const alerts = Array.isArray(psrAlertsRef.current)
        ? psrAlertsRef.current.length
        : 0;

      const total = pendingPayments + activePromises + pendingA2a + alerts;

      if (total === 0) {
        return {
          count: 0,
          message: "Ops Task Board is fully clear — nothing pending.",
        };
      }

      const parts = [];

      if (pendingPayments > 0) {
        parts.push(
          `${pendingPayments} payment${pendingPayments === 1 ? "" : "s"}`,
        );
      }

      if (activePromises > 0) {
        parts.push(
          `${activePromises} promise${activePromises === 1 ? "" : "s"}`,
        );
      }

      if (pendingA2a > 0) {
        parts.push(
          `${pendingA2a} A2A settlement${pendingA2a === 1 ? "" : "s"}`,
        );
      }

      if (alerts > 0) {
        parts.push(`${alerts} PSR alert${alerts === 1 ? "" : "s"}`);
      }

      return {
        count: total,
        message: `${total} item${
          total === 1 ? "" : "s"
        } waiting on the Ops Task Board — ${parts.join(", ")}.`,
      };
    }

    function firePulse() {
      setKanbanPulse(buildKanbanPulse());
      setKanbanPulseVisible(true);

      if (kanbanPulseHideTimerRef.current) {
        window.clearTimeout(kanbanPulseHideTimerRef.current);
      }

      kanbanPulseHideTimerRef.current = window.setTimeout(() => {
        setKanbanPulseVisible(false);
      }, 6000);
    }

    const PULSE_INTERVAL_MS = 30 * 60 * 1000; // every 30 minutes

    const interval = window.setInterval(firePulse, PULSE_INTERVAL_MS);

    return () => {
      window.clearInterval(interval);

      if (kanbanPulseHideTimerRef.current) {
        window.clearTimeout(kanbanPulseHideTimerRef.current);
      }
    };
  }, []);

  /* ==========================================================
     Payment Route Health — real per-channel recovery rate,
     computed from the same `cases` array the table renders,
     not a fabricated number.
  ========================================================== */

  const channelHealth = useMemo(() => {
    const groups = {};

    cases.forEach((item) => {
      const key = item.channel || "unknown";

      if (!groups[key]) {
        groups[key] = { total: 0, recovered: 0 };
      }

      groups[key].total += 1;

      if (String(item.outcome || "").toUpperCase() === "RECOVERED") {
        groups[key].recovered += 1;
      }
    });

    return Object.entries(groups)
      .map(([channel, stats]) => ({
        channel,
        rate: stats.total > 0 ? stats.recovered / stats.total : 0,
        total: stats.total,
      }))
      .sort((a, b) => b.total - a.total)
      .slice(0, 7);
  }, [cases]);

  const overallRouteHealth = useMemo(() => {
    if (channelHealth.length === 0) return 0;

    const sum = channelHealth.reduce((acc, c) => acc + c.rate, 0);

    return sum / channelHealth.length;
  }, [channelHealth]);

  const latestLivePayment = useMemo(() => {
    if (!Array.isArray(livePayments) || livePayments.length === 0) {
      return null;
    }

    return [...livePayments].sort((a, b) => {
      const timeA = new Date(a.timestamp || 0).getTime();
      const timeB = new Date(b.timestamp || 0).getTime();

      return timeB - timeA;
    })[0];
  }, [livePayments]);

  /* ==========================================================
     Filter Cases
  ========================================================== */

  const filteredCases = useMemo(() => {
    const query = search.trim().toLowerCase();

    return cases.filter((item) => {
      const matchesSearch =
        !query ||
        String(item.case_id || "")
          .toLowerCase()
          .includes(query) ||
        String(item.customer_id || "")
          .toLowerCase()
          .includes(query) ||
        String(item.root_cause || "")
          .toLowerCase()
          .includes(query) ||
        String(item.action || "")
          .toLowerCase()
          .includes(query) ||
        String(item.channel || "")
          .toLowerCase()
          .includes(query) ||
        String(item.surface || "")
          .toLowerCase()
          .includes(query);

      const matchesDecision =
        decisionFilter === "ALL" ||
        String(item.roi_decision || "").toUpperCase() === decisionFilter;

      const matchesOutcome =
        outcomeFilter === "ALL" ||
        String(item.outcome || "").toUpperCase() === outcomeFilter;

      return matchesSearch && matchesDecision && matchesOutcome;
    });
  }, [cases, search, decisionFilter, outcomeFilter]);

  /* ==========================================================
     Case Pagination
  ========================================================== */

  const totalCasesPages = Math.max(
    1,
    Math.ceil(filteredCases.length / PAGE_SIZE),
  );

  const paginatedCases = useMemo(() => {
    const start = (casesPage - 1) * PAGE_SIZE;

    return filteredCases.slice(start, start + PAGE_SIZE);
  }, [filteredCases, casesPage]);

  /* ==========================================================
     Ledger Pagination
  ========================================================== */

  const totalLedgerPages = Math.max(1, Math.ceil(ledger.length / PAGE_SIZE));

  const paginatedLedger = useMemo(() => {
    const start = (ledgerPage - 1) * PAGE_SIZE;

    return ledger.slice(start, start + PAGE_SIZE).map((event, index) => ({
      event,
      globalIndex: start + index,
    }));
  }, [ledger, ledgerPage]);

  /* ==========================================================
     Filter A2A
  ========================================================== */

  const filteredA2a = useMemo(() => {
    if (a2aFilter === "ALL") {
      return a2aSettlements;
    }

    return a2aSettlements.filter((item) => getA2aOutcome(item) === a2aFilter);
  }, [a2aSettlements, a2aFilter]);

  const liveA2aStats = useMemo(() => {
    const agreements = liveA2aSettlements.length;
    const agreed = liveA2aSettlements.filter(
      (item) => String(item.settlement_status || "").toUpperCase() === "AGREED",
    ).length;
    const pending = liveA2aSettlements.filter(
      (item) =>
        !item.recovery_confirmed &&
        String(item.payment_status || "").toUpperCase() === "PENDING",
    ).length;
    const confirmed = liveA2aSettlements.filter(
      (item) => item.recovery_confirmed === true,
    ).length;

    return { agreements, agreed, pending, confirmed };
  }, [liveA2aSettlements]);

  /* ==========================================================
     Recovery Pulse — derived only from live data already loaded
  ========================================================== */

  const pulseEvents = useMemo(() => {
    const events = [];

    (Array.isArray(livePayments) ? livePayments : []).forEach((item, index) => {
      const event = normalizePulseEvent(item, "payment", index);
      if (event) events.push(event);
    });

    (Array.isArray(promises) ? promises : []).forEach((item, index) => {
      const event = normalizePulseEvent(item, "promise", index);
      if (event) events.push(event);
    });

    (Array.isArray(liveA2aSettlements) ? liveA2aSettlements : []).forEach(
      (item, index) => {
        const event = normalizePulseEvent(item, "a2a", index);
        if (event) events.push(event);
      },
    );

    (Array.isArray(ledger) ? ledger : [])
      .slice(0, 12)
      .forEach((item, index) => {
        const event = normalizePulseEvent(item, "ledger", index);
        if (event) events.push(event);
      });

    return events
      .sort((a, b) => {
        const timeA = new Date(a.timestamp || 0).getTime();
        const timeB = new Date(b.timestamp || 0).getTime();
        return timeB - timeA;
      })
      .slice(0, 8);
  }, [livePayments, promises, liveA2aSettlements, ledger]);

  const filteredPulseEvents = useMemo(() => {
    if (pulseFilter === "ALL") return pulseEvents;

    return pulseEvents.filter((event) => {
      if (pulseFilter === "PAYMENTS") return event.type === "payment";
      if (pulseFilter === "PROMISES") return event.type === "promise";
      if (pulseFilter === "A2A") return event.type === "a2a";
      return true;
    });
  }, [pulseEvents, pulseFilter]);

  const nextBestAction = useMemo(() => {
    const livePending = (Array.isArray(livePayments) ? livePayments : []).find(
      (item) => {
        const status = String(
          item.status || item.payment_status || item.outcome || "",
        ).toUpperCase();

        return (
          status === "FAILED" ||
          status === "PENDING" ||
          status === "PENDING_RECOVERY"
        );
      },
    );

    if (livePending) {
      return {
        kind: "LIVE",
        caseId: livePending.case_id,
        title: "Review live recovery",
        detail: "A live payment still needs operator attention.",
        amount: livePending.amount,
        target: "live-performance",
      };
    }

    const promised = (Array.isArray(promises) ? promises : []).find(
      (item) => String(item.status || "").toUpperCase() === "PROMISED",
    );

    if (promised) {
      return {
        kind: "PROMISE",
        caseId: promised.case_id,
        title: "Review Promise-to-Pay",
        detail: "A customer promise is currently active.",
        amount: promised.promised_amount,
        target: "promises",
      };
    }

    const pendingA2a = (
      Array.isArray(liveA2aSettlements) ? liveA2aSettlements : []
    ).find((item) => {
      return (
        !item.recovery_confirmed &&
        String(item.payment_status || "").toUpperCase() === "PENDING"
      );
    });

    if (pendingA2a) {
      return {
        kind: "A2A",
        caseId: pendingA2a.case_id,
        title: "Review A2A settlement",
        detail: "A negotiated settlement is awaiting payment confirmation.",
        amount: pendingA2a.agreed_amount,
        target: "live-a2a",
      };
    }

    return {
      kind: "MONITOR",
      caseId: null,
      title: "Recovery engine is clear",
      detail: "No pending live action was detected in the loaded state.",
      amount: null,
      target: "recovery-cases",
    };
  }, [livePayments, promises, liveA2aSettlements]);

  function openPulseEvent(event) {
    if (!event) return;

    if (event.caseId) {
      const caseMatch = cases.find(
        (item) => String(item.case_id) === String(event.caseId),
      );

      if (caseMatch) {
        setSelectedCase(caseMatch);
        return;
      }
    }

    if (event.type === "promise") {
      document.getElementById("promises")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
      return;
    }

    if (event.type === "a2a") {
      document.getElementById("live-a2a")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
      return;
    }

    document.getElementById("live-performance")?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }

  function openNextBestAction() {
    document.getElementById(nextBestAction.target)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }

  /* ==========================================================
     Loading
  ========================================================== */

  if (loading) {
    const launchGreeting = getTimeGreeting();

    return (
      <div className="loading-screen">
        <div className="launch-grid" aria-hidden="true" />

        <div className="money-rain" aria-hidden="true">
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
          <span>₹</span>
        </div>

        <div className="loading-orbit" aria-hidden="true">
          <div className="loading-logo">R</div>
        </div>

        <div className="launch-copy">
          <div className="launch-eyebrow">REVIVE v1.1 · COMMAND CENTER</div>
          <h2>
            {launchGreeting}, {user?.name || "there"}.
          </h2>
          <p>Preparing your revenue intelligence workspace.</p>

          <div className="launch-value">
            <span>RECOVERED REVENUE</span>
            <strong>{formatCurrency(loadingAmount)}</strong>
          </div>

          <div className="launch-progress" aria-hidden="true">
            <span />
          </div>

          <small>Sense · Decide · Act · Prove</small>
        </div>
      </div>
    );
  }

  const summary = dashboard?.summary || {};

  const derivedA2aEligible = a2aSettlements.filter((item) =>
    getA2aEligibility(item),
  ).length;

  const derivedA2aSettled = a2aSettlements.filter(
    (item) => getA2aOutcome(item) === "SETTLED",
  ).length;

  const backendA2aEligible = Number(
    summary.a2a_eligible_cases ?? summary.a2a_eligible ?? 0,
  );

  const backendA2aSettled = Number(
    summary.a2a_settled_cases ?? summary.a2a_settled ?? 0,
  );

  const a2aEligible =
    derivedA2aEligible > 0 ? derivedA2aEligible : backendA2aEligible;

  const a2aSettled =
    derivedA2aSettled > 0 ? derivedA2aSettled : backendA2aSettled;

  const settlementRate =
    a2aEligible > 0 ? ((a2aSettled / a2aEligible) * 100).toFixed(2) : "0.00";

  /* ==========================================================
     Explanation Data
  ========================================================== */

  const explanationData = explanation?.data || null;

  const explanationBody = explanationData?.explanation || null;

  const explanationEvidence = explanationData?.evidence || null;

  const explanationCase = explanationEvidence?.case || null;

  const explanationDecision = explanationEvidence?.decision || null;

  const explanationRoi = explanationEvidence?.roi || null;

  const explanationPolicy = explanationEvidence?.policy || null;

  const explanationRecovery = explanationEvidence?.recovery || null;

  const explanationLedger = explanationEvidence?.ledger || null;

  /* ==========================================================
     Simulation Data
  ========================================================== */

  const simulation = simulationResult?.simulation || null;

  const simulationMetrics = simulation?.metrics || {};

  const simulationComparison = simulation?.comparison_to_current || {};

  const currentSimulationMetrics = dashboard?.metrics || {};

  const simulatedCases = Array.isArray(simulation?.cases)
    ? simulation.cases
    : [];

  const simulatedPsrAlerts = Array.isArray(simulation?.psr_alerts)
    ? simulation.psr_alerts
    : [];

  const simulatedA2a = Array.isArray(simulation?.a2a_settlements)
    ? simulation.a2a_settlements
    : [];

  const simulatedLedger = Array.isArray(simulation?.ledger)
    ? simulation.ledger
    : [];

  /* ==========================================================
     SIMULATION BUSINESS INSIGHT
  ========================================================== */

  const simulationInsight = (() => {
    if (!simulation) {
      return null;
    }

    const recoveredRevenueDiff = Number(
      simulationComparison.recovered_revenue_difference ?? 0,
    );

    const recoveryRateDiff = Number(
      simulationComparison.recovery_rate_difference ?? 0,
    );

    const recoveryCostDiff = Number(
      simulationComparison.recovery_cost_difference ?? 0,
    );

    const netRecoveredValueDiff = Number(
      simulationComparison.net_recovered_value_difference ?? 0,
    );

    const pursuedCasesDiff = Number(
      simulationComparison.pursued_cases_difference ?? 0,
    );

    const stoppedCasesDiff = Number(
      simulationComparison.stopped_cases_difference ?? 0,
    );

    const formatAbsCurrency = (value) =>
      formatCurrency(Math.abs(Number(value) || 0));

    /*
     * Net recovered value is the primary economic signal.
     * Recovery cost is treated as a trade-off, not automatically
     * as a benefit when it decreases.
     */

    let verdict = "CURRENT POLICY RECOMMENDED";
    let verdictClass = "current";
    let title = "Current policy remains economically stronger.";
    let message = "";

    if (netRecoveredValueDiff > 0) {
      verdict = "WHAT-IF POLICY RECOMMENDED";
      verdictClass = "what-if";
      title = "What-if policy creates more net recovered value.";

      if (recoveryCostDiff > 0 && recoveredRevenueDiff > 0) {
        message = `The simulated policy recovers ${formatAbsCurrency(
          recoveredRevenueDiff,
        )} more revenue for ${formatAbsCurrency(
          recoveryCostDiff,
        )} more recovery cost, increasing net recovered value by ${formatAbsCurrency(
          netRecoveredValueDiff,
        )}.`;
      } else if (recoveredRevenueDiff > 0) {
        message = `The simulated policy adds ${formatAbsCurrency(
          recoveredRevenueDiff,
        )} in recovered revenue and improves net recovered value by ${formatAbsCurrency(
          netRecoveredValueDiff,
        )}.`;
      } else {
        message = `The simulated policy improves net recovered value by ${formatAbsCurrency(
          netRecoveredValueDiff,
        )}.`;
      }
    } else if (netRecoveredValueDiff < 0) {
      verdict = "CURRENT POLICY RECOMMENDED";
      verdictClass = "current";
      title = "What-if policy reduces net recovered value.";

      if (recoveryCostDiff < 0 && recoveredRevenueDiff < 0) {
        message = `The simulated policy saves ${formatAbsCurrency(
          recoveryCostDiff,
        )} in recovery cost, but sacrifices ${formatAbsCurrency(
          recoveredRevenueDiff,
        )} in recovered revenue, reducing net recovered value by ${formatAbsCurrency(
          netRecoveredValueDiff,
        )}.`;
      } else if (recoveredRevenueDiff < 0) {
        message = `The simulated policy loses ${formatAbsCurrency(
          recoveredRevenueDiff,
        )} in recovered revenue and reduces net recovered value by ${formatAbsCurrency(
          netRecoveredValueDiff,
        )}.`;
      } else {
        message = `The simulated policy reduces net recovered value by ${formatAbsCurrency(
          netRecoveredValueDiff,
        )}.`;
      }
    } else if (recoveredRevenueDiff > 0) {
      verdict = "NEUTRAL NET IMPACT";
      verdictClass = "neutral";
      title = "More revenue, but no net-value improvement.";
      message = `The simulated policy changes recovered revenue by ${formatAbsCurrency(
        recoveredRevenueDiff,
      )}, but produces no net recovered value improvement.`;
    } else if (recoveredRevenueDiff < 0) {
      verdict = "CURRENT POLICY RECOMMENDED";
      verdictClass = "current";
      title = "Current policy remains the safer choice.";
      message = `The simulated policy reduces recovered revenue by ${formatAbsCurrency(
        recoveredRevenueDiff,
      )} without improving net recovered value.`;
    } else {
      verdict = "NO MATERIAL CHANGE";
      verdictClass = "neutral";
      title = "This policy produces the same economics.";
      message =
        "The simulation produced the same recovery economics as the current policy.";
    }

    return {
      verdict,
      verdictClass,
      title,
      message,
      recoveredRevenueDiff,
      recoveryRateDiff,
      recoveryCostDiff,
      netRecoveredValueDiff,
      pursuedCasesDiff,
      stoppedCasesDiff,
    };
  })();

  const lastRunLabel = formatRelativeTime(
    batchHistory.length > 0
      ? batchHistory[batchHistory.length - 1].recorded_at
      : null,
  );

  /* ==========================================================
     Main UI
  ========================================================== */

  return (
    <div className="app">
      {/* ======================================================
          SIDEBAR
      ====================================================== */}

      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">R</div>

          <div>
            <div className="brand-name">REVIVE</div>

            <div className="brand-subtitle">AI REVENUE RECOVERY</div>
          </div>
        </div>

        <nav className="navigation">
          <a href="#overview">
            <span>⊞</span>
            <span>Overview</span>
          </a>

          <a href="#cases">
            <span>▤</span>
            <span>Recovery Cases</span>
          </a>

          <a href="#psr">
            <span>🛡</span>
            <span>PSR Guardian</span>
          </a>

          <a href="#simulator">
            <span>✳</span>
            <span>What-If Simulator</span>
          </a>

          <a href="#a2a">
            <span>⛓</span>
            <span>A2A Settlement</span>
          </a>

          <a href="#live-a2a">
            <span>↔</span>
            <span>Live A2A</span>
          </a>

          <a href="#payments">
            <span>$</span>
            <span>Live Payments</span>
          </a>

          <a href="#live-performance">
            <span>↗</span>
            <span>Live Recovery</span>
          </a>

          <a href="#promises">
            <span>✓</span>
            <span>Promise Tracker</span>
          </a>

          <a href="#ledger">
            <span>▥</span>
            <span>Recovery Ledger</span>
          </a>
        </nav>

        <div className="sidebar-status">
          <span className="online-dot" />

          <div>
            <strong>System Online</strong>

            <small>All engines operational</small>
          </div>
        </div>
      </aside>

      {/* ======================================================
          MAIN
      ====================================================== */}

      <div className="app-content">
        <main className="main">
          {/* TOPBAR */}

          <header className="topbar">
            <div>
              <div className="eyebrow">REVIVE v1.1</div>

              <div className="page-title">REVENUE INTELLIGENCE</div>
            </div>

            <div className="topbar-actions">
              <button
                type="button"
                className="export-button"
                onClick={exportBoardReport}
                disabled={exportingReport || runningBatch}
              >
                {exportingReport ? "Generating PDF..." : "↓ Export snapshot"}
              </button>

              <button
                type="button"
                className="run-button"
                onClick={runRecoveryBatch}
                disabled={runningBatch || exportingReport}
              >
                {runningBatch ? "Running..." : "↻ Run recovery batch"}
              </button>

              <div className="topbar-icon-group">
                <div className="notification-bell-wrap" ref={notificationRef}>
                  <button
                    type="button"
                    className="notification-bell"
                    onClick={() => setNotificationsOpen((v) => !v)}
                    aria-label="Notifications"
                  >
                    <svg
                      width="18"
                      height="18"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
                      <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
                    </svg>
                    {notificationsList.filter(
                      (n) => normalizeSeverity(n.severity) !== "info",
                    ).length > 0 && (
                      <span className="notification-badge">
                        {
                          notificationsList.filter(
                            (n) => normalizeSeverity(n.severity) !== "info",
                          ).length
                        }
                      </span>
                    )}
                  </button>

                  {notificationsOpen && (
                    <div className="notification-panel">
                      <div className="notification-panel-header">
                        <span>NOTIFICATIONS</span>

                        <button
                          type="button"
                          className="notification-close"
                          onClick={() => setNotificationsOpen(false)}
                        >
                          ×
                        </button>
                      </div>

                      <div className="notification-list">
                        {notificationsList.length === 0 ? (
                          <div className="notification-empty">
                            Run a batch to see alerts here.
                          </div>
                        ) : (
                          notificationsList.map((n, i) => (
                            <div
                              key={
                                n.id ||
                                `${n.notification_type || "notif"}-${
                                  n.case_id || i
                                }`
                              }
                              className={`notification-item notification-${normalizeSeverity(
                                n.severity,
                              )}`}
                            >
                              <div className="notification-title">
                                {n.title}
                              </div>

                              <div className="notification-message">
                                {n.message}
                              </div>
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  )}
                </div>

                <div className="kanban-icon-wrap">
                  <button
                    type="button"
                    className="notification-bell kanban-toolbar-button"
                    onClick={() => {
                      setKanbanOpen(true);
                      setKanbanPulseVisible(false);
                    }}
                    aria-label="Open Ops Task Board"
                    title="Ops Task Board"
                  >
                    <svg
                      width="18"
                      height="18"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <rect x="3" y="4" width="18" height="16" rx="2" />
                      <line x1="9" y1="4" x2="9" y2="20" />
                      <line x1="15" y1="4" x2="15" y2="20" />
                    </svg>

                    {kanbanPulse && kanbanPulse.count > 0 && (
                      <span className="notification-badge">
                        {kanbanPulse.count}
                      </span>
                    )}
                  </button>

                  {kanbanPulse && (
                    <div
                      className={`kanban-pulse-toast${
                        kanbanPulseVisible ? " visible" : ""
                      }`}
                      role="status"
                      aria-live="polite"
                    >
                      <div className="kanban-pulse-arrow" aria-hidden="true" />

                      <span className="kanban-pulse-icon">▦</span>

                      <div className="kanban-pulse-copy">
                        <strong>Manage your tasks with the Kanban board</strong>
                        <span>{kanbanPulse.message}</span>
                      </div>

                      <button
                        type="button"
                        className="kanban-pulse-action"
                        onClick={() => {
                          setKanbanPulseVisible(false);
                          setKanbanOpen(true);
                        }}
                      >
                        View →
                      </button>

                      <button
                        type="button"
                        className="kanban-pulse-dismiss"
                        onClick={() => setKanbanPulseVisible(false)}
                        aria-label="Dismiss"
                      >
                        ×
                      </button>
                    </div>
                  )}
                </div>
              </div>

              <ThemeToggle />

              <div className="topbar-profile-slot">
                <UserMenu />
              </div>
            </div>
          </header>

          {error && <div className="error-banner">⚠ {error}</div>}

          {/* ====================================================
            OVERVIEW
        ==================================================== */}

          <section id="overview">
            <div className="hero">
              <div>
                <div className="hero-kicker">COMMAND CENTER</div>

                <h1>
                  Recover revenue.
                  <br />
                  <span className="dim">Prove every decision.</span>
                </h1>

                <p>
                  Revive senses why payments fail, decides what is worth
                  pursuing, and acts with an auditable recovery policy.
                </p>

                <div className="hero-meta">
                  <span>⚡ {summary.total_cases || 0} cases analyzed</span>
                  <span>↝ Last run {lastRunLabel}</span>
                </div>
              </div>

              <div className="hero-gauge">
                <div className="hero-gauge-label">ROI-GATED</div>

                <div className="hero-gauge-ring">
                  <div
                    className="hero-gauge-track"
                    style={{
                      "--gauge-percent": Math.max(
                        0,
                        Math.min(
                          100,
                          (Number(summary.recovery_rate) || 0) * 100,
                        ),
                      ),
                    }}
                  >
                    <div className="hero-gauge-circle">
                      <strong>{formatPercent(summary.recovery_rate)}</strong>
                      <span>RECOVERY RATE</span>
                    </div>
                  </div>
                </div>

                <div className="hero-gauge-footer">SYNTHETIC BENCHMARK</div>
              </div>
            </div>

            <div className="hero-flow-pulse-row">
              <div className="hero-flow">
                <div className="hero-flow-header">
                  <div>
                    <div className="hero-flow-kicker">RECOVERY FLOW</div>
                    <strong>Sense → Decide → Act → Prove</strong>
                  </div>
                  <span className="hero-flow-status">
                    <span className="hero-flow-dot" />
                    OPERATIONAL
                  </span>
                </div>

                <div className="hero-flow-steps">
                  <button
                    type="button"
                    className="hero-flow-step"
                    onClick={() =>
                      document.getElementById("cases")?.scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      })
                    }
                  >
                    <span className="hero-flow-index">01</span>
                    <span className="hero-flow-copy">
                      <strong>SENSE</strong>
                      <small>{summary.total_cases || 0} cases analyzed</small>
                    </span>
                    <span className="hero-flow-arrow">↗</span>
                  </button>

                  <button
                    type="button"
                    className="hero-flow-step"
                    onClick={() =>
                      document.getElementById("overview")?.scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      })
                    }
                  >
                    <span className="hero-flow-index">02</span>
                    <span className="hero-flow-copy">
                      <strong>DECIDE</strong>
                      <small>
                        {summary.pursued_cases || 0} recovery actions pursued
                      </small>
                    </span>
                    <span className="hero-flow-arrow">↗</span>
                  </button>

                  <button
                    type="button"
                    className="hero-flow-step"
                    onClick={() =>
                      document.getElementById("payments")?.scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      })
                    }
                  >
                    <span className="hero-flow-index">03</span>
                    <span className="hero-flow-copy">
                      <strong>ACT</strong>
                      <small>
                        {summary.recovered_cases || 0} recoveries completed
                      </small>
                    </span>
                    <span className="hero-flow-arrow">↗</span>
                  </button>

                  <button
                    type="button"
                    className="hero-flow-step"
                    onClick={() =>
                      document.getElementById("ledger")?.scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      })
                    }
                  >
                    <span className="hero-flow-index">04</span>
                    <span className="hero-flow-copy">
                      <strong>PROVE</strong>
                      <small>
                        {summary.ledger_events || ledger.length || 0} ledger
                        events recorded
                      </small>
                    </span>
                    <span className="hero-flow-arrow">↗</span>
                  </button>
                </div>
              </div>

              <div className="recovery-pulse">
                <div className="pulse-header">
                  <div>
                    <div className="pulse-kicker">
                      <span className="pulse-live-dot" />
                      RECOVERY PULSE
                    </div>
                    <strong>Operator focus</strong>
                  </div>

                  <span className="pulse-count">
                    {pulseEvents.length} events
                  </span>
                </div>

                <div
                  className="pulse-filters"
                  role="tablist"
                  aria-label="Recovery activity filters"
                >
                  {["ALL", "PAYMENTS", "PROMISES", "A2A"].map((filter) => (
                    <button
                      key={filter}
                      type="button"
                      className={pulseFilter === filter ? "active" : ""}
                      onClick={() => setPulseFilter(filter)}
                      role="tab"
                      aria-selected={pulseFilter === filter}
                    >
                      {filter}
                    </button>
                  ))}
                </div>

                <div className="pulse-next">
                  <div className="pulse-next-label">NEXT BEST ACTION</div>
                  <div className="pulse-next-title">{nextBestAction.title}</div>
                  <div className="pulse-next-detail">
                    {nextBestAction.detail}
                  </div>

                  <div className="pulse-next-footer">
                    {nextBestAction.amount !== null ? (
                      <strong>{formatCurrency(nextBestAction.amount)}</strong>
                    ) : (
                      <span>MONITOR</span>
                    )}

                    <button type="button" onClick={openNextBestAction}>
                      Inspect →
                    </button>
                  </div>
                </div>

                <div className="pulse-activity">
                  {filteredPulseEvents.length === 0 ? (
                    <div className="pulse-empty">
                      No activity in this filter yet.
                    </div>
                  ) : (
                    filteredPulseEvents.slice(0, 3).map((event) => (
                      <button
                        type="button"
                        className="pulse-event"
                        key={event.id}
                        onClick={() => openPulseEvent(event)}
                      >
                        <span
                          className={`pulse-event-icon pulse-${event.type}`}
                        >
                          {event.type === "payment"
                            ? "₹"
                            : event.type === "promise"
                              ? "✓"
                              : event.type === "a2a"
                                ? "↔"
                                : "•"}
                        </span>

                        <span className="pulse-event-copy">
                          <strong>{event.title}</strong>
                          <small>
                            {event.meta}
                            {event.timestamp
                              ? ` · ${formatRelativeTime(event.timestamp)}`
                              : ""}
                          </small>
                        </span>

                        {event.amount !== null && (
                          <span className="pulse-event-amount">
                            {formatCurrency(event.amount)}
                          </span>
                        )}
                      </button>
                    ))
                  )}
                </div>
              </div>
            </div>

            {/* KPI */}

            <div className="kpi-grid">
              <div className="kpi-card">
                <div className="kpi-label">ADDRESSABLE REVENUE</div>

                <div className="kpi-value">
                  {formatCurrency(summary.addressable_revenue)}
                </div>

                <div className="kpi-meta">
                  {summary.total_cases || 0} recovery cases
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">RECOVERED REVENUE</div>

                <div className="kpi-value">
                  {formatCurrency(summary.recovered_revenue)}
                </div>

                <div className="kpi-meta positive">
                  ↑ {formatPercent(summary.recovery_rate)} of addressable
                  revenue
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">RECOVERY RATE</div>

                <div className="kpi-value">
                  {formatPercent(summary.recovery_rate)}
                </div>

                <div className="kpi-meta">
                  {summary.recovered_cases || 0} cases recovered
                </div>
              </div>

              <div className="kpi-card">
                <div className="kpi-label">NET RECOVERED VALUE</div>

                <div className="kpi-value">
                  {formatCurrency(summary.net_recovered_value)}
                </div>

                <div className="kpi-meta">
                  Cost: {formatCurrency(summary.recovery_cost)}
                </div>
              </div>
            </div>

            {/* PERFORMANCE */}

            <div className="section-card">
              <div className="section-heading">
                <div>
                  <div className="section-kicker">
                    PERFORMANCE / CURRENT BATCH
                  </div>

                  <h2>Recovery performance</h2>
                </div>

                <span className="live-badge">BENCHMARK</span>
              </div>

              <div className="performance">
                <div className="performance-rate">
                  {formatPercent(summary.recovery_rate)}

                  <span>
                    of addressable revenue
                    <br />
                    recovered
                  </span>
                </div>

                <div className="performance-bar">
                  <div className="progress-track">
                    <div
                      className="progress-fill"
                      style={{
                        width: `${Math.min(
                          100,
                          Math.max(0, Number(summary.recovery_rate || 0) * 100),
                        )}%`,
                      }}
                    />
                  </div>

                  <div className="progress-scale">
                    <span>₹0</span>
                    <span>{formatCurrency(summary.recovered_revenue)}</span>
                    <span>{formatCurrency(summary.addressable_revenue)}</span>
                  </div>
                </div>

                <div className="performance-details">
                  <div>
                    <strong>RECOVERED</strong>

                    <span>{formatCurrency(summary.recovered_revenue)}</span>
                  </div>

                  <div>
                    <strong>UNRECOVERED</strong>

                    <span>{formatCurrency(summary.unrecovered_revenue)}</span>
                  </div>

                  <div>
                    <strong>COST / ₹ RECOVERED</strong>

                    <span>
                      ₹
                      {Number(summary.cost_per_rupee_recovered || 0).toFixed(3)}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* RECOVERY POLICY PERFORMANCE */}
            {/*
            Replaces the old "Last Seven Batches" trend chart. That chart
            read from batch_history.json, which contains repeated batch
            entries from re-running the same 105-case benchmark — so the
            "trend" it showed wasn't real progression. This panel instead
            shows three real policy outcomes side by side: the live
            Current policy (from the authoritative benchmark) plus
            Conservative and Balanced, both produced by live calls to the
            same /api/simulate engine the What-If Simulator uses.
          */}

            <div className="section-card policy-performance-card">
              <div className="section-heading">
                <div>
                  <div className="section-kicker">POLICY INTELLIGENCE</div>

                  <h2>Recovery policy performance</h2>

                  <p className="section-description">
                    Conservative, Current, and Balanced — three real policy
                    configurations run through the same benchmark, via the same
                    simulation engine as the What-If Simulator.
                  </p>
                </div>

                <button
                  type="button"
                  className="close-button reset-button policy-performance-refresh"
                  onClick={loadPolicyComparison}
                  disabled={policyComparisonLoading}
                >
                  {policyComparisonLoading ? "Running..." : "↻ Refresh"}
                </button>
              </div>

              {policyComparisonError && (
                <div className="error-banner simulator-error">
                  ⚠ {policyComparisonError}
                </div>
              )}

              {policyComparisonLoading && !policyComparison ? (
                <div className="policy-performance-loading">
                  Running Conservative and Balanced policies through the
                  engine...
                </div>
              ) : (
                (() => {
                  const currentRate = Number(summary.recovery_rate || 0);
                  const currentNetValue = Number(
                    summary.net_recovered_value || 0,
                  );

                  const conservativeRate = Number(
                    policyComparison?.conservative?.metrics?.recovery_rate ?? 0,
                  );

                  const balancedMetrics = policyComparison?.balanced?.metrics;
                  const balancedRate = Number(
                    balancedMetrics?.recovery_rate ?? 0,
                  );
                  const balancedNetValue = Number(
                    balancedMetrics?.net_recovered_value ?? 0,
                  );

                  const rows = [
                    {
                      key: "conservative",
                      label: POLICY_PRESETS.conservative.label,
                      sublabel: POLICY_PRESETS.conservative.description,
                      rate: conservativeRate,
                      hasData: Boolean(policyComparison?.conservative),
                    },
                    {
                      key: "current",
                      label: "Current",
                      sublabel: "The live, authoritative policy.yaml.",
                      rate: currentRate,
                      hasData: true,
                    },
                    {
                      key: "balanced",
                      label: POLICY_PRESETS.balanced.label,
                      sublabel: POLICY_PRESETS.balanced.description,
                      rate: balancedRate,
                      hasData: Boolean(policyComparison?.balanced),
                    },
                  ].filter((row) => row.hasData);

                  const bestRow = rows.reduce(
                    (best, row) => (!best || row.rate > best.rate ? row : best),
                    null,
                  );

                  const chartRows = rows.map((row) => ({
                    ...row,
                    recommended:
                      bestRow?.key === row.key && row.key !== "current",
                  }));

                  return (
                    <>
                      <PolicyPerformanceBars items={chartRows} />

                      {bestRow &&
                        bestRow.key === "balanced" &&
                        policyComparison?.balanced && (
                          <div className="roi-summary-line policy-performance-summary">
                            {POLICY_PRESETS.balanced.label} gives{" "}
                            <strong
                              className={
                                balancedNetValue - currentNetValue >= 0
                                  ? "positive"
                                  : "negative"
                              }
                            >
                              {balancedNetValue - currentNetValue >= 0
                                ? "+"
                                : "−"}
                              {formatCurrency(
                                Math.abs(balancedNetValue - currentNetValue),
                              )}
                            </strong>{" "}
                            net recovered value versus the current policy.
                          </div>
                        )}
                    </>
                  );
                })()
              )}
            </div>

            {/* ROI COMPARISON */}

            {dashboard?.metrics?.naive_comparison && (
              <div className="section-card">
                <div className="section-heading">
                  <div>
                    <div className="section-kicker">ROI PORTFOLIO ENGINE</div>

                    <h2>What the stopping rule is actually worth</h2>

                    <p className="section-description">
                      Same {cases.length} cases, two strategies: a naive policy
                      that retries every case until it succeeds or exhausts its
                      attempts, versus Revive's expected-value-gated strategy
                      that stops chasing a case the moment its expected value
                      turns negative.
                    </p>
                  </div>

                  <span className="live-badge">BENCHMARK</span>
                </div>

                <div className="kpi-grid">
                  <div className="kpi-card">
                    <div className="kpi-label">NAIVE STRATEGY RECOVERS</div>

                    <div className="kpi-value">
                      {formatCurrency(
                        dashboard.metrics.naive_comparison
                          .naive_recovered_amount,
                      )}
                    </div>

                    <div className="kpi-meta">
                      {dashboard.metrics.naive_comparison.naive_attempts}{" "}
                      attempts, cost{" "}
                      {formatCurrency(
                        dashboard.metrics.naive_comparison.naive_cost,
                      )}
                    </div>
                  </div>

                  <div className="kpi-card">
                    <div className="kpi-label">REVIVE RECOVERS</div>

                    <div className="kpi-value">
                      {formatCurrency(
                        dashboard.metrics.naive_comparison
                          .revive_recovered_amount,
                      )}
                    </div>

                    <div className="kpi-meta">
                      {dashboard.metrics.naive_comparison.revive_attempts}{" "}
                      attempts, cost{" "}
                      {formatCurrency(
                        dashboard.metrics.naive_comparison.revive_cost,
                      )}
                    </div>
                  </div>

                  <div className="kpi-card">
                    <div className="kpi-label">ADDITIONAL RECOVERY</div>

                    <div className="kpi-value">
                      {formatCurrency(
                        dashboard.metrics.naive_comparison.additional_recovery,
                      )}
                    </div>

                    <div className="kpi-meta positive">
                      from pursuing positive-EV cases naive retry gives up on
                    </div>
                  </div>

                  <div className="kpi-card">
                    <div className="kpi-label">
                      {dashboard.metrics.naive_comparison.additional_cost > 0
                        ? "ADDITIONAL COST"
                        : "COST SAVINGS"}
                    </div>

                    <div className="kpi-value">
                      {formatCurrency(
                        Math.abs(
                          dashboard.metrics.naive_comparison.additional_cost,
                        ),
                      )}
                    </div>

                    <div className="kpi-meta">
                      for{" "}
                      {formatCurrency(
                        dashboard.metrics.naive_comparison.additional_recovery,
                      )}{" "}
                      more recovered
                    </div>
                  </div>
                </div>

                <div className="roi-summary-line">
                  {dashboard.metrics.naive_comparison.summary}
                </div>
              </div>
            )}

            {latestLivePayment && (
              <div className="section-card real-capture-card">
                <div className="section-heading">
                  <div>
                    <div className="section-kicker">REAL-WORLD VALIDATION</div>

                    <h2>
                      Verified against Razorpay's actual test-mode sandbox
                    </h2>

                    <p className="section-description">
                      This card always reflects the latest real Razorpay Test
                      Mode payment received through the verified webhook flow.
                      The payment is tracked independently from the synthetic
                      105-case benchmark.
                    </p>
                  </div>

                  <span className="live-badge">LIVE</span>
                </div>

                <div className="real-capture-grid">
                  <div>
                    <div className="kpi-label">RAZORPAY PAYMENT ID</div>

                    <div className="real-capture-value mono">
                      {latestLivePayment.razorpay_payment_id || "—"}
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">CUSTOMER</div>

                    <div className="real-capture-value">
                      {latestLivePayment.customer_name || "—"}
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">AMOUNT</div>

                    <div className="real-capture-value">
                      {formatCurrency(latestLivePayment.amount)}
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">ROOT CAUSE</div>

                    <div className="real-capture-value">
                      {latestLivePayment.root_cause_label || "—"}
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">PAYMENT METHOD</div>

                    <div className="real-capture-value">
                      {latestLivePayment.payment_method || "—"}
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">RECOVERY STATUS</div>

                    <div className="real-capture-value">
                      <StatusBadge
                        value={
                          latestLivePayment.recovery_status ||
                          latestLivePayment.outcome ||
                          "PENDING_RECOVERY"
                        }
                      />
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">RECOVERED AMOUNT</div>

                    <div className="real-capture-value">
                      {formatCurrency(latestLivePayment.recovered_amount || 0)}
                    </div>
                  </div>

                  <div>
                    <div className="kpi-label">RECOVERY SOURCE</div>

                    <div className="real-capture-value mono">
                      {latestLivePayment.recovery_source || "—"}
                    </div>
                  </div>
                </div>

                <div className="roi-summary-line">
                  {latestLivePayment.recovery_status === "RECOVERED"
                    ? `Real Razorpay payment ${latestLivePayment.razorpay_payment_id || ""} was received through the verified webhook flow and successfully recovered for ${formatCurrency(
                        latestLivePayment.recovered_amount || 0,
                      )}.`
                    : `Real Razorpay payment ${latestLivePayment.razorpay_payment_id || ""} was received through the verified webhook flow and is currently awaiting recovery.`}
                </div>
              </div>
            )}
          </section>

          {/* ====================================================
            RECOVERY CASES
        ==================================================== */}

          <section id="cases" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">RECOVERY OPERATIONS</div>

                <h2>Recovery cases</h2>

                <p className="section-description">
                  Every case carries its reason, economic signal, and next-best
                  action.
                </p>
              </div>

              <div className="case-count">
                {filteredCases.length} shown / {cases.length} total
              </div>
            </div>

            <div className="filters">
              <input
                type="text"
                placeholder="Search case, customer, root cause..."
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />

              <Dropdown
                value={decisionFilter}
                onChange={setDecisionFilter}
                options={[
                  { value: "ALL", label: "All decisions" },
                  { value: "PURSUE", label: "Pursue" },
                  { value: "STOP", label: "Stop" },
                ]}
              />

              <Dropdown
                value={outcomeFilter}
                onChange={setOutcomeFilter}
                options={[
                  { value: "ALL", label: "All outcomes" },
                  { value: "RECOVERED", label: "Recovered" },
                  { value: "NOT_RECOVERED", label: "Not Recovered" },
                ]}
              />
            </div>

            <div className="table-card">
              <div className="table-wrapper">
                <table>
                  <thead>
                    <tr>
                      <th>CASE / CUSTOMER</th>
                      <th>AMOUNT</th>
                      <th>ROOT CAUSE</th>
                      <th>DECISION</th>
                      <th>CHANNEL</th>
                      <th>OUTCOME</th>
                      <th aria-hidden="true"></th>
                    </tr>
                  </thead>

                  <tbody>
                    {paginatedCases.length > 0 ? (
                      paginatedCases.map((item) => {
                        const type = stopType(item);

                        return (
                          <tr key={item.case_id} onClick={() => openCase(item)}>
                            <td>
                              <strong>{item.case_id}</strong>

                              <small>
                                {item.customer_id}
                                {item.surface
                                  ? ` · ${item.surface
                                      .replace(/_/g, " ")
                                      .replace(/\b\w/g, (c) =>
                                        c.toUpperCase(),
                                      )}`
                                  : ""}
                              </small>
                            </td>

                            <td>{formatCurrency(item.amount)}</td>

                            <td>
                              {item.root_cause}

                              <small>
                                p(success) {formatPercent(item.roi_probability)}
                              </small>
                            </td>

                            <td>
                              <StatusBadge value={item.roi_decision} />

                              {type && (
                                <small
                                  className="decision-reason"
                                  title={stopReason(item)}
                                >
                                  {stopReason(item)}
                                </small>
                              )}
                            </td>

                            <td>{item.channel}</td>

                            <td>
                              <StatusBadge value={item.outcome} />
                            </td>

                            <td className="row-chevron" aria-hidden="true">
                              ›
                            </td>
                          </tr>
                        );
                      })
                    ) : (
                      <tr>
                        <td colSpan="7" className="table-empty">
                          No recovery cases match the selected filters.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              <Pagination
                page={casesPage}
                totalPages={totalCasesPages}
                onChange={setCasesPage}
                totalItems={filteredCases.length}
                pageSize={PAGE_SIZE}
              />
            </div>
          </section>

          {/* ====================================================
            PSR
        ==================================================== */}

          <section id="psr" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">SYSTEMIC RISK</div>

                <h2>PSR Guardian</h2>

                <p className="section-description">
                  Payment System Resilience detects route-level patterns before
                  they compound.
                </p>
              </div>

              <span className="live-badge alert-count-badge">
                {psrAlerts.length} active alert
                {psrAlerts.length === 1 ? "" : "s"}
              </span>
            </div>

            {psrAlerts.length > 0 ? (
              psrAlerts.map((alert, index) => (
                <div
                  className="alert-card"
                  key={`${alert.bank}-${alert.card_network}-${index}`}
                >
                  <div className="alert-header">
                    <div className="alert-icon">🛡</div>

                    <div>
                      <StatusBadge value={alert.severity || "HIGH"} />
                    </div>

                    <div className="alert-id">
                      PSR-{String(index + 1).padStart(3, "0")}
                    </div>
                  </div>

                  <h3 className="alert-title">Systemic route degradation</h3>

                  <p className="alert-summary">
                    {alert.concentrated_cases} of {alert.group_size}{" "}
                    {String(alert.decline_code || "").replace(/_/g, " ")}{" "}
                    failures on {alert.bank} / {alert.card_network} concentrated
                    inside one window starting {formatDate(alert.window_start)}.
                  </p>

                  <div className="alert-grid">
                    <div>
                      <span>ROUTE</span>

                      <strong>
                        {alert.bank}
                        {" / "}
                        {alert.card_network}
                      </strong>
                    </div>

                    <div>
                      <span>CONCENTRATION</span>

                      <strong>
                        {formatPercent(alert.concentration_ratio)} concentration
                      </strong>
                    </div>

                    <div>
                      <span>DETECTION</span>

                      <strong>PSR stream analysis</strong>
                    </div>
                  </div>

                  <div className="alert-recommendation">
                    <span>RECOMMENDED ACTION</span>

                    <p>{alert.recommendation}</p>
                  </div>

                  {Array.isArray(alert.evidence) &&
                    alert.evidence.length > 0 && (
                      <div className="evidence-box">
                        <div className="evidence-title">Evidence</div>

                        {alert.evidence.map((evidence, evidenceIndex) => (
                          <div className="evidence-item" key={evidenceIndex}>
                            {typeof evidence === "object"
                              ? JSON.stringify(evidence)
                              : String(evidence)}
                          </div>
                        ))}
                      </div>
                    )}
                </div>
              ))
            ) : (
              <EmptyState>
                ✓ No systemic payment route anomalies detected.
              </EmptyState>
            )}
          </section>

          {/* ====================================================
            WHAT-IF SIMULATOR
        ==================================================== */}

          <section id="simulator" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">POLICY INTELLIGENCE</div>

                <h2>What-if policy simulator</h2>

                <p className="section-description">
                  Explore the economics of a different recovery policy without
                  touching the authoritative rules.
                </p>
              </div>

              <span className="live-badge">SANDBOX</span>
            </div>

            {/* CONTROLS */}

            <div className="section-card">
              <div className="simulator-grid">
                <div className="simulator-field">
                  <label>MAX CONTACT ATTEMPTS</label>

                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={simulationOverrides.max_contact_attempts}
                    onChange={(event) =>
                      updateSimulationOverride(
                        "max_contact_attempts",
                        event.target.value,
                      )
                    }
                  />

                  <small>Maximum recovery contacts per case.</small>
                </div>

                <div className="simulator-field">
                  <label>MAX DISCOUNT %</label>

                  <input
                    type="number"
                    min="0"
                    max="100"
                    step="1"
                    value={simulationOverrides.max_discount_percent}
                    onChange={(event) =>
                      updateSimulationOverride(
                        "max_discount_percent",
                        event.target.value,
                      )
                    }
                  />

                  <small>Maximum negotiation discount.</small>
                </div>

                <div className="simulator-field">
                  <label>MAX NEGOTIATION ROUNDS</label>

                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={simulationOverrides.max_negotiation_rounds}
                    onChange={(event) =>
                      updateSimulationOverride(
                        "max_negotiation_rounds",
                        event.target.value,
                      )
                    }
                  />

                  <small>Maximum A2A negotiation rounds.</small>
                </div>

                <div className="simulator-field">
                  <label>COOLDOWN HOURS</label>

                  <input
                    type="number"
                    min="0"
                    step="1"
                    value={simulationOverrides.cooldown_hours}
                    onChange={(event) =>
                      updateSimulationOverride(
                        "cooldown_hours",
                        event.target.value,
                      )
                    }
                  />

                  <small>Time between recovery attempts.</small>
                </div>

                <div className="simulator-field">
                  <label>RETRY MAX ATTEMPTS</label>

                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={simulationOverrides.retry_max_attempts}
                    onChange={(event) =>
                      updateSimulationOverride(
                        "retry_max_attempts",
                        event.target.value,
                      )
                    }
                  />

                  <small>Maximum ROI retry attempts.</small>
                </div>
              </div>

              <div className="simulator-actions">
                <button
                  className="run-button"
                  onClick={runPolicySimulation}
                  disabled={runningSimulation}
                >
                  {runningSimulation
                    ? "Simulating..."
                    : "▷ Run what-if simulation"}
                </button>

                <button
                  className="close-button reset-button"
                  onClick={resetSimulation}
                  disabled={runningSimulation}
                >
                  Reset
                </button>
              </div>

              {runningSimulation && (
                <div className="simulator-progress" aria-hidden="true">
                  <div className="simulator-progress-fill" />
                </div>
              )}

              {simulationError && (
                <div className="error-banner simulator-error">
                  ⚠ {simulationError}
                </div>
              )}
            </div>

            {/* ==================================================
              SIMULATION RESULT
          ================================================== */}

            {simulation && (
              <div key={simulationRunId} className="simulation-result-enter">
                <div className="section-card simulation-result-card">
                  <div className="section-heading">
                    <div>
                      <div className="section-kicker">SIMULATION RESULT</div>

                      <h2>Current Policy vs What-If Policy</h2>

                      <p className="section-description">
                        The values below come directly from a fresh
                        RevivePipeline execution using a temporary in-memory
                        policy copy.
                      </p>
                    </div>

                    <span className="live-badge">SIMULATED</span>
                  </div>

                  {/* EFFECTIVE POLICY */}

                  <div className="simulator-policy-grid">
                    <div>
                      <span>CONTACT ATTEMPTS</span>

                      <strong>
                        {simulation.effective_policy?.max_contact_attempts}
                      </strong>
                    </div>

                    <div>
                      <span>MAX DISCOUNT</span>

                      <strong>
                        {simulation.effective_policy?.max_discount_percent}%
                      </strong>
                    </div>

                    <div>
                      <span>NEGOTIATION ROUNDS</span>

                      <strong>
                        {simulation.effective_policy?.max_negotiation_rounds}
                      </strong>
                    </div>

                    <div>
                      <span>COOLDOWN</span>

                      <strong>
                        {simulation.effective_policy?.cooldown_hours} h
                      </strong>
                    </div>

                    <div>
                      <span>RETRY ATTEMPTS</span>

                      <strong>
                        {simulation.effective_policy?.retry_max_attempts}
                      </strong>
                    </div>
                  </div>

                  {/* COMPARISON */}

                  <div className="simulation-comparison">
                    {/* RECOVERED REVENUE */}

                    <div className="simulation-comparison-card">
                      <span>RECOVERED REVENUE</span>

                      <strong className="simulation-main-value">
                        {formatCurrency(simulationMetrics.recovered_revenue)}
                      </strong>

                      <small className="simulation-comparison-meta">
                        <span>
                          Current:{" "}
                          {formatCurrency(
                            currentSimulationMetrics.recovered_revenue,
                          )}
                        </span>

                        <span
                          className={differenceClass(
                            simulationComparison.recovered_revenue_difference,
                          )}
                        >
                          Δ{" "}
                          {formatDifference(
                            simulationComparison.recovered_revenue_difference,
                          )}
                        </span>
                      </small>
                    </div>

                    {/* RECOVERY RATE */}

                    <div className="simulation-comparison-card">
                      <span>RECOVERY RATE</span>

                      <strong className="simulation-main-value">
                        {formatPercent(simulationMetrics.recovery_rate)}
                      </strong>

                      <small className="simulation-comparison-meta">
                        <span>
                          Current:{" "}
                          {formatPercent(
                            currentSimulationMetrics.recovery_rate,
                          )}
                        </span>

                        <span
                          className={differenceClass(
                            simulationComparison.recovery_rate_difference,
                          )}
                        >
                          Δ{" "}
                          {formatDifference(
                            simulationComparison.recovery_rate_difference,
                            "percent",
                          )}
                        </span>
                      </small>
                    </div>

                    {/* COST */}

                    <div className="simulation-comparison-card">
                      <span>RECOVERY COST</span>

                      <strong className="simulation-main-value">
                        {formatCurrency(simulationMetrics.recovery_cost)}
                      </strong>

                      <small
                        className={`simulation-comparison-meta ${
                          Number(
                            simulationComparison.recovery_cost_difference,
                          ) > 0
                            ? "negative"
                            : Number(
                                  simulationComparison.recovery_cost_difference,
                                ) < 0
                              ? "positive"
                              : "neutral-change"
                        }`}
                      >
                        <span>
                          Current:{" "}
                          {formatCurrency(
                            currentSimulationMetrics.recovery_cost,
                          )}
                        </span>

                        <span>
                          Δ{" "}
                          {formatDifference(
                            simulationComparison.recovery_cost_difference,
                          )}
                        </span>
                      </small>
                    </div>

                    {/* NET VALUE */}

                    <div className="simulation-comparison-card">
                      <span>NET RECOVERED VALUE</span>

                      <strong className="simulation-main-value">
                        {formatCurrency(simulationMetrics.net_recovered_value)}
                      </strong>

                      <small className="simulation-comparison-meta">
                        <span>
                          Current:{" "}
                          {formatCurrency(
                            currentSimulationMetrics.net_recovered_value,
                          )}
                        </span>

                        <span
                          className={differenceClass(
                            simulationComparison.net_recovered_value_difference,
                          )}
                        >
                          Δ{" "}
                          {formatDifference(
                            simulationComparison.net_recovered_value_difference,
                          )}
                        </span>
                      </small>
                    </div>

                    {/* PURSUED CASES */}

                    <div className="simulation-comparison-card">
                      <span>PURSUED CASES</span>

                      <strong className="simulation-main-value">
                        {simulationMetrics.pursued_cases ?? 0}
                      </strong>

                      <small className="simulation-comparison-meta">
                        <span>
                          Current: {currentSimulationMetrics.pursued_cases ?? 0}
                        </span>

                        <span
                          className={differenceClass(
                            simulationComparison.pursued_cases_difference,
                          )}
                        >
                          Δ{" "}
                          {formatDifference(
                            simulationComparison.pursued_cases_difference,
                            "integer",
                          )}
                        </span>
                      </small>
                    </div>

                    {/* STOPPED CASES */}

                    <div className="simulation-comparison-card">
                      <span>STOPPED CASES</span>

                      <strong className="simulation-main-value">
                        {simulationMetrics.stopped_cases ?? 0}
                      </strong>

                      <small
                        className={`simulation-comparison-meta ${
                          Number(
                            simulationComparison.stopped_cases_difference,
                          ) < 0
                            ? "positive"
                            : Number(
                                  simulationComparison.stopped_cases_difference,
                                ) > 0
                              ? "negative"
                              : "neutral-change"
                        }`}
                      >
                        <span>
                          Current: {currentSimulationMetrics.stopped_cases ?? 0}
                        </span>

                        <span>
                          Δ{" "}
                          {formatDifference(
                            simulationComparison.stopped_cases_difference,
                            "integer",
                          )}
                        </span>
                      </small>
                    </div>
                  </div>

                  {/* ==========================================================
    POLICY VERDICT
========================================================== */}

                  {simulationInsight && (
                    <div
                      className={`simulation-verdict simulation-verdict-${simulationInsight.verdictClass}`}
                    >
                      <div className="simulation-verdict-header">
                        <div>
                          <div className="simulation-verdict-kicker">
                            POLICY VERDICT
                          </div>

                          <strong className="simulation-verdict-title">
                            {simulationInsight.title}
                          </strong>
                        </div>

                        <span className="simulation-verdict-badge">
                          {simulationInsight.verdict}
                        </span>
                      </div>

                      <p className="simulation-verdict-message">
                        {simulationInsight.message}
                      </p>

                      <div className="simulation-verdict-metrics">
                        <div>
                          <span>RECOVERED REVENUE</span>

                          <strong
                            className={differenceClass(
                              simulationInsight.recoveredRevenueDiff,
                            )}
                          >
                            {formatDifference(
                              simulationInsight.recoveredRevenueDiff,
                            )}
                          </strong>
                        </div>

                        <div>
                          <span>RECOVERY RATE</span>

                          <strong
                            className={differenceClass(
                              simulationInsight.recoveryRateDiff,
                            )}
                          >
                            {formatDifference(
                              simulationInsight.recoveryRateDiff,
                              "percent",
                            )}
                          </strong>
                        </div>

                        <div>
                          <span>RECOVERY COST</span>

                          <strong
                            className={
                              simulationInsight.recoveryCostDiff > 0
                                ? "negative"
                                : simulationInsight.recoveryCostDiff < 0
                                  ? "positive"
                                  : "neutral-change"
                            }
                          >
                            {formatDifference(
                              simulationInsight.recoveryCostDiff,
                            )}
                          </strong>
                        </div>

                        <div>
                          <span>NET VALUE</span>

                          <strong
                            className={differenceClass(
                              simulationInsight.netRecoveredValueDiff,
                            )}
                          >
                            {formatDifference(
                              simulationInsight.netRecoveredValueDiff,
                            )}
                          </strong>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* RESULT SUMMARY */}

                  <div className="roi-summary-line">
                    Simulation produced{" "}
                    <strong>
                      {formatCurrency(simulationMetrics.recovered_revenue)}
                    </strong>{" "}
                    recovered revenue at a recovery rate of{" "}
                    <strong>
                      {formatPercent(simulationMetrics.recovery_rate)}
                    </strong>
                    .
                  </div>
                </div>

                {/* SIMULATED CASES */}

                <div
                  className="section-card"
                  style={{
                    marginTop: "16px",
                  }}
                >
                  <div className="section-heading">
                    <div>
                      <div className="section-kicker">SIMULATED PORTFOLIO</div>

                      <h2>Simulated Recovery Cases</h2>

                      <p className="section-description">
                        Cases generated by the actual recovery engine under the
                        temporary policy.
                      </p>
                    </div>

                    <div className="case-count">
                      {simulatedCases.length} cases
                    </div>
                  </div>

                  {simulatedCases.length > 0 ? (
                    <div className="table-card">
                      <div className="table-wrapper">
                        <table>
                          <thead>
                            <tr>
                              <th>CASE</th>
                              <th>AMOUNT</th>
                              <th>ACTION</th>
                              <th>P(SUCCESS)</th>
                              <th>EV</th>
                              <th>DECISION</th>
                              <th>OUTCOME</th>
                              <th>RECOVERED</th>
                            </tr>
                          </thead>

                          <tbody>
                            {simulatedCases.slice(0, 25).map((item) => (
                              <tr key={item.case_id}>
                                <td>
                                  <strong>{item.case_id}</strong>

                                  <small>{item.customer_id}</small>
                                </td>

                                <td>{formatCurrency(item.amount)}</td>

                                <td>{item.action}</td>

                                <td>{formatPercent(item.roi_probability)}</td>

                                <td>{formatCurrency(item.expected_value)}</td>

                                <td>
                                  <StatusBadge value={item.roi_decision} />
                                </td>

                                <td>
                                  <StatusBadge value={item.outcome} />
                                </td>

                                <td>{formatCurrency(item.recovered_amount)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ) : (
                    <EmptyState>No simulated cases returned.</EmptyState>
                  )}

                  {simulatedCases.length > 25 && (
                    <div
                      className="section-description"
                      style={{
                        marginTop: "12px",
                      }}
                    >
                      Showing the first 25 of {simulatedCases.length} simulated
                      cases.
                    </div>
                  )}
                </div>

                {/* SIMULATION SYSTEM OUTPUT */}

                <div className="a2a-stats">
                  <div>
                    <strong>{simulatedPsrAlerts.length}</strong>

                    <span>Simulated PSR Alerts</span>
                  </div>

                  <div>
                    <strong>{simulatedA2a.length}</strong>

                    <span>Simulated A2A Results</span>
                  </div>

                  <div>
                    <strong>{simulatedLedger.length}</strong>

                    <span>Simulated Ledger Events</span>
                  </div>
                </div>
              </div>
            )}
          </section>

          {/* ====================================================
            A2A
        ==================================================== */}

          <section id="a2a" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">AGENT COMMERCE</div>

                <h2>Autonomous settlement</h2>

                <p className="section-description">
                  Merchant and payer agents negotiate settlement within policy
                  boundaries — no human in the loop.
                </p>
              </div>

              <div className="case-count">
                {filteredA2a.length}
                {" / "}
                {a2aSettlements.length}
              </div>
            </div>

            <div className="a2a-stats">
              <div>
                <strong>{a2aEligible}</strong>

                <span>ELIGIBLE CASES</span>
              </div>

              <div>
                <strong>{a2aSettled}</strong>

                <span>SETTLED AUTOMATICALLY</span>
              </div>

              <div>
                <strong>{settlementRate}%</strong>

                <span>SETTLEMENT RATE</span>
              </div>
            </div>

            <div className="filters a2a-filters">
              <Dropdown
                value={a2aFilter}
                onChange={setA2aFilter}
                options={[
                  { value: "ALL", label: "All settlements" },
                  { value: "SETTLED", label: "Settled" },
                  { value: "REJECTED", label: "Rejected" },
                  { value: "BLOCKED", label: "Blocked" },
                ]}
              />
            </div>

            {filteredA2a.length > 0 ? (
              <div className="table-card">
                <div className="table-wrapper">
                  <table className="a2a-table">
                    <thead>
                      <tr>
                        <th>CASE</th>
                        <th>INVOICE</th>
                        <th>ELIGIBILITY</th>
                        <th>OUTCOME</th>
                        <th>FINAL AMOUNT</th>
                        <th>DISCOUNT</th>
                        <th>ROUNDS</th>
                        <th>DETAILS</th>
                      </tr>
                    </thead>

                    <tbody>
                      {filteredA2a.map((settlement, index) => (
                        <tr
                          key={settlement.case_id || index}
                          onClick={() => setSelectedSettlement(settlement)}
                        >
                          <td>
                            <strong>{settlement.case_id}</strong>
                          </td>

                          <td>{settlement.invoice_id}</td>

                          <td>
                            <StatusBadge
                              value={
                                settlement.eligible ? "ELIGIBLE" : "BLOCKED"
                              }
                            />
                          </td>

                          <td>
                            <StatusBadge value={settlement.outcome} />
                          </td>

                          <td>{formatCurrency(settlement.final_amount)}</td>

                          <td>
                            {Number(settlement.discount_percent || 0).toFixed(
                              2,
                            )}
                            %
                          </td>

                          <td>{settlement.rounds}</td>

                          <td>
                            <span className="view-link">View transcript ↗</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <EmptyState>
                No A2A settlements match the selected filter.
              </EmptyState>
            )}
          </section>

          {/* ====================================================
            LIVE A2A AGENT COMMERCE
        ==================================================== */}

          <section id="live-a2a" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">LIVE AGENT COMMERCE</div>

                <h2>Live A2A settlement</h2>

                <p className="section-description">
                  Real recovery cases can negotiate with an independent payer
                  agent, reach an agreement, and remain pending until a verified
                  Razorpay capture confirms the recovery.
                </p>
              </div>

              <span className="live-badge">LIVE A2A</span>
            </div>

            <div className="live-a2a-stats">
              <div>
                <strong>{liveA2aStats.agreements}</strong>
                <span>AGREEMENTS</span>
              </div>
              <div>
                <strong>{liveA2aStats.agreed}</strong>
                <span>AGREED</span>
              </div>
              <div>
                <strong>{liveA2aStats.pending}</strong>
                <span>PAYMENT PENDING</span>
              </div>
              <div>
                <strong className="positive">{liveA2aStats.confirmed}</strong>
                <span>RECOVERY CONFIRMED</span>
              </div>
            </div>

            {liveA2aError && (
              <div className="error-banner live-a2a-error">
                <strong>A2A action could not complete.</strong> {liveA2aError}
              </div>
            )}

            {liveA2aSettlements.length > 0 ? (
              <div className="live-a2a-list">
                {liveA2aSettlements.map((settlement) => {
                  const confirmed = settlement.recovery_confirmed === true;
                  const pending =
                    !confirmed &&
                    String(settlement.payment_status || "").toUpperCase() ===
                      "PENDING";

                  return (
                    <div
                      className={`live-a2a-card${confirmed ? " confirmed" : ""}`}
                      key={settlement.agreement_id || settlement.case_id}
                    >
                      <div className="live-a2a-card-top">
                        <div>
                          <div className="live-a2a-label">LIVE AGREEMENT</div>
                          <h3>{settlement.case_id}</h3>
                          <p>
                            {settlement.invoice_id || "Invoice unavailable"}
                          </p>
                        </div>
                        <span
                          className={`live-a2a-status ${confirmed ? "confirmed" : pending ? "pending" : "agreed"}`}
                        >
                          {confirmed
                            ? "✓ CONFIRMED"
                            : pending
                              ? "PAYMENT PENDING"
                              : "AGREED"}
                        </span>
                      </div>

                      <div className="live-a2a-amount-row">
                        <div>
                          <span>AGREED AMOUNT</span>
                          <strong>
                            {formatCurrency(settlement.agreed_amount)}
                          </strong>
                        </div>
                        <div>
                          <span>PAYER AGENT</span>
                          <strong className="mono">
                            {settlement.payer_agent_id || "—"}
                          </strong>
                        </div>
                      </div>

                      <div className="live-a2a-flow">
                        <div className="live-a2a-step done">
                          <span>✓</span>
                          <strong>NEGOTIATED</strong>
                        </div>
                        <div className="live-a2a-line" />
                        <div className="live-a2a-step done">
                          <span>✓</span>
                          <strong>AGREED</strong>
                        </div>
                        <div className="live-a2a-line" />
                        <div
                          className={`live-a2a-step ${confirmed ? "done" : "active"}`}
                        >
                          <span>{confirmed ? "✓" : "2"}</span>
                          <strong>PAYMENT</strong>
                        </div>
                        <div className="live-a2a-line" />
                        <div
                          className={`live-a2a-step ${confirmed ? "done" : "waiting"}`}
                        >
                          <span>{confirmed ? "✓" : "4"}</span>
                          <strong>RECOVERY</strong>
                        </div>
                      </div>

                      <div className="live-a2a-meta">
                        <div>
                          <span>SETTLEMENT</span>
                          <StatusBadge
                            value={settlement.settlement_status || "AGREED"}
                          />
                        </div>
                        <div>
                          <span>PAYMENT</span>
                          <StatusBadge
                            value={settlement.payment_status || "PENDING"}
                          />
                        </div>
                        <div>
                          <span>RECOVERY</span>
                          <StatusBadge
                            value={confirmed ? "CONFIRMED" : "PENDING"}
                          />
                        </div>
                      </div>

                      <div className="live-a2a-card-actions">
                        <button
                          type="button"
                          className="view-link-button"
                          onClick={() => setSelectedLiveA2a(settlement)}
                        >
                          View settlement →
                        </button>

                        {settlement.payment_url && !confirmed && (
                          <button
                            type="button"
                            className="run-button live-a2a-payment-button"
                            onClick={() =>
                              window.open(
                                settlement.payment_url,
                                "_blank",
                                "noopener,noreferrer",
                              )
                            }
                          >
                            Open payment link ↗
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="live-a2a-empty">
                <div className="live-a2a-empty-icon">↔</div>
                <strong>No live A2A agreements yet</strong>
                <p>
                  Eligible B2B recovery cases with an independent AP agent will
                  appear here.
                </p>
              </div>
            )}

            {livePayments.some(isLiveA2aEligible) && (
              <div className="live-a2a-opportunities">
                <div className="live-a2a-opportunities-header">
                  <div>
                    <div className="live-a2a-label">
                      ELIGIBLE RECOVERY OPPORTUNITIES
                    </div>
                    <strong>Autonomous AP negotiation</strong>
                  </div>
                  <span>
                    {livePayments.filter(isLiveA2aEligible).length} eligible
                  </span>
                </div>

                <div className="live-a2a-opportunity-list">
                  {livePayments.filter(isLiveA2aEligible).map((item) => {
                    const existing = getLiveA2aForCase(item.case_id);
                    const busy = a2aActionCaseId === item.case_id;

                    return (
                      <div className="live-a2a-opportunity" key={item.case_id}>
                        <div>
                          <strong>{item.customer_name || item.case_id}</strong>
                          <span>
                            {item.invoice_id} · {formatCurrency(item.amount)}
                          </span>
                        </div>
                        {existing ? (
                          <button
                            type="button"
                            className="view-link-button"
                            onClick={() => setSelectedLiveA2a(existing)}
                          >
                            View agreement →
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="run-button"
                            onClick={() => startLiveA2aSettlement(item)}
                            disabled={busy}
                          >
                            {busy ? "Negotiating…" : "⚡ Start A2A settlement"}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </section>

          {/* ====================================================
            LIVE RAZORPAY PAYMENTS
        ==================================================== */}

          <section id="payments" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">PAYMENT SURFACES</div>

                <h2>Live payments</h2>

                <p className="section-description">
                  Payment links and captured failures land here for immediate
                  diagnosis.
                </p>
              </div>

              <div className="case-count">
                {livePayments.length} live failures
              </div>
            </div>

            <div className="live-payments-grid">
              <div className="section-card">
                <div className="live-payments-icon">$</div>

                <div className="kpi-label">LIVE PAYMENT FAILURES</div>

                <div className="kpi-value">{livePayments.length}</div>

                <p className="section-description" style={{ marginTop: "8px" }}>
                  Real Razorpay failures are kept separate from the 105-case
                  synthetic benchmark.
                </p>
              </div>

              <div className="section-card">
                <div className="live-payments-icon">📶</div>

                <div className="kpi-label">PAYMENT ROUTE HEALTH</div>

                <div className="kpi-value">
                  {formatPercent(overallRouteHealth)}
                </div>

                <p className="section-description" style={{ marginTop: "8px" }}>
                  Synthetic recovery performance by channel across the current
                  105-case benchmark.
                </p>

                {channelHealth.length > 0 && (
                  <div className="route-health-bars">
                    {channelHealth.map((c) => (
                      <div
                        key={c.channel}
                        className={`route-health-bar${
                          c.rate < 0.4
                            ? " danger"
                            : c.rate < 0.7
                              ? " warning"
                              : ""
                        }`}
                        title={`${c.channel}: ${formatPercent(c.rate)} recovered`}
                      />
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="section-card">
              <div className="simulator-grid">
                <div className="simulator-field">
                  <label>AMOUNT (₹)</label>

                  <input
                    type="number"
                    min="1"
                    value={checkoutForm.amount}
                    onChange={(event) =>
                      setCheckoutForm((prev) => ({
                        ...prev,
                        amount: event.target.value,
                      }))
                    }
                  />

                  <small>Real Razorpay test-mode payment link.</small>
                </div>

                <div className="simulator-field">
                  <label>CUSTOMER NAME</label>

                  <input
                    type="text"
                    value={checkoutForm.customer_name}
                    onChange={(event) =>
                      setCheckoutForm((prev) => ({
                        ...prev,
                        customer_name: event.target.value,
                      }))
                    }
                  />

                  <small>Shown on the Razorpay checkout page.</small>
                </div>

                <div className="simulator-field">
                  <label>CUSTOMER EMAIL</label>

                  <input
                    type="email"
                    list="known-customer-emails"
                    placeholder="customer@example.com"
                    value={checkoutForm.customer_email}
                    onChange={(event) => {
                      const email = event.target.value;
                      const match = knownCustomers.find(
                        (c) => c.email === email,
                      );

                      setCheckoutForm((prev) => ({
                        ...prev,
                        customer_email: email,
                        customer_name:
                          match?.name && !prev.customer_name
                            ? match.name
                            : prev.customer_name,
                      }));
                    }}
                  />

                  <datalist id="known-customer-emails">
                    {knownCustomers
                      .filter((c) => c.email)
                      .map((c) => (
                        <option key={c.customer_id} value={c.email} />
                      ))}
                  </datalist>

                  <small>
                    This is how the customer is actually reached — used to send
                    the payment link and any recovery alerts, and to recognize
                    repeat customers automatically.
                  </small>
                </div>

                <div className="simulator-field">
                  <label>CUSTOMER ID (optional)</label>

                  <input
                    type="text"
                    placeholder="Leave blank to auto-match by email"
                    value={checkoutForm.customer_id}
                    onChange={(event) =>
                      setCheckoutForm((prev) => ({
                        ...prev,
                        customer_id: event.target.value,
                      }))
                    }
                  />

                  <small>
                    Only needed to tag an existing customer record. Left blank,
                    Revive resolves (or creates) the ID from the email above.
                  </small>
                </div>

                <div className="simulator-field">
                  <label>SURFACE</label>

                  <Dropdown
                    value={checkoutForm.surface}
                    onChange={(value) =>
                      setCheckoutForm((prev) => ({
                        ...prev,
                        surface: value,
                        // Clear B2B-only fields when switching away so a
                        // stale invoice_id/has_ap_agent can't leak into an
                        // unrelated surface.
                        ...(value !== "b2b_receivable"
                          ? { invoice_id: "", has_ap_agent: false, disputed: false }
                          : {}),
                      }))
                    }
                    options={[
                      {
                        value: "subscription_failure",
                        label: "Subscription failure",
                      },
                      { value: "b2b_receivable", label: "B2B receivable" },
                    ]}
                  />

                  <small>
                    Only "B2B receivable" cases are eligible for Live A2A
                    settlement.
                  </small>
                </div>

                {checkoutForm.surface === "b2b_receivable" && (
                  <>
                    <div className="simulator-field">
                      <label>INVOICE ID</label>

                      <input
                        type="text"
                        placeholder="INV-1001"
                        value={checkoutForm.invoice_id}
                        onChange={(event) =>
                          setCheckoutForm((prev) => ({
                            ...prev,
                            invoice_id: event.target.value,
                          }))
                        }
                      />

                      <small>Required for A2A eligibility.</small>
                    </div>

                    <div className="simulator-field checkbox-field">
                      <label>
                        <input
                          type="checkbox"
                          checked={checkoutForm.has_ap_agent}
                          onChange={(event) =>
                            setCheckoutForm((prev) => ({
                              ...prev,
                              has_ap_agent: event.target.checked,
                            }))
                          }
                        />
                        Payer has an AP agent (A2A eligible)
                      </label>
                    </div>

                    <div className="simulator-field checkbox-field">
                      <label>
                        <input
                          type="checkbox"
                          checked={checkoutForm.disputed}
                          onChange={(event) =>
                            setCheckoutForm((prev) => ({
                              ...prev,
                              disputed: event.target.checked,
                            }))
                          }
                        />
                        Invoice is disputed (blocks A2A negotiation)
                      </label>
                    </div>
                  </>
                )}
              </div>

              <div className="simulator-actions">
                <button
                  className="run-button"
                  onClick={createLiveCheckout}
                  disabled={creatingCheckout}
                >
                  {creatingCheckout ? "Creating..." : "◇ Create Razorpay Link"}
                </button>

                <button
                  className="close-button reset-button"
                  onClick={resetLivePayments}
                  disabled={resettingLiveCases}
                >
                  {resettingLiveCases ? "Resetting..." : "Reset live cases"}
                </button>
              </div>

              {checkoutError && (
                <div className="error-banner simulator-error">
                  ⚠ {checkoutError}
                </div>
              )}

              <p className="section-description" style={{ marginTop: "10px" }}>
                Opens Razorpay's real checkout in a new tab. In test mode, use
                UPI ID <code>failure@razorpay</code> to trigger a real,
                deterministic failure — it appears below within a few seconds
                once the webhook fires.
              </p>
            </div>

            {livePayments.length > 0 ? (
              <div className="table-card">
                <div className="table-wrapper">
                  <table className="a2a-table">
                    <thead>
                      <tr>
                        <th>CASE</th>
                        <th>CUSTOMER</th>
                        <th>AMOUNT</th>
                        <th>ROOT CAUSE</th>
                        <th>METHOD</th>
                        <th>STATUS</th>
                        <th>RAZORPAY PAYMENT ID</th>
                        <th>TIME</th>
                        <th>ACTIONS</th>
                      </tr>
                    </thead>

                    <tbody>
                      {livePayments.map((item) => (
                        <tr
                          key={item.case_id}
                          onClick={() => openLivePayment(item)}
                        >
                          <td>
                            <strong>{item.case_id}</strong>
                          </td>

                          <td>{item.customer_name}</td>

                          <td>{formatCurrency(item.amount)}</td>

                          <td>
                            <StatusBadge value={item.root_cause_label} />
                          </td>

                          <td>{item.payment_method}</td>

                          <td>
                            <StatusBadge
                              value={
                                item.recovery_status ||
                                item.outcome ||
                                "PENDING_RECOVERY"
                              }
                            />
                          </td>

                          <td>{item.razorpay_payment_id}</td>

                          <td>{formatDate(item.timestamp)}</td>

                          <td>
                            {item.recovery_status === "PENDING_RECOVERY" ? (
                              <button
                                className="run-button"
                                style={{
                                  padding: "6px 12px",
                                  fontSize: "12px",
                                }}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  retryLivePayment(item.case_id);
                                }}
                                disabled={retryingCaseId === item.case_id}
                              >
                                {retryingCaseId === item.case_id
                                  ? "Opening..."
                                  : "↻ Retry Payment"}
                              </button>
                            ) : (
                              <span style={{ opacity: 0.5 }}>—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <EmptyState>
                No live Razorpay failures detected yet. Create a checkout link
                above and fail it to see a real case appear here.
              </EmptyState>
            )}

            {retryError && (
              <div
                className="error-banner simulator-error"
                style={{ marginTop: "10px" }}
              >
                ⚠ {retryError}
              </div>
            )}
          </section>

          {/* ====================================================
            LIVE RAZORPAY RECOVERY PERFORMANCE
        ==================================================== */}

          <section id="live-performance" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">LIVE RECOVERY PERFORMANCE</div>

                <h2>Live Razorpay recovery</h2>

                <p className="section-description">
                  Operational recovery performance from real Razorpay Test Mode
                  webhook events. This layer is completely separate from the
                  synthetic 105-case benchmark.
                </p>
              </div>

              <span className="live-badge">LIVE / TEST MODE</span>
            </div>

            {liveMetrics ? (
              <>
                {/* LIVE KPI CARDS */}

                <div className="live-recovery-kpi-grid">
                  <div className="live-recovery-kpi">
                    <div className="kpi-label">LIVE RECOVERY RATE</div>

                    <div className="live-recovery-kpi-value positive">
                      {formatPercent(liveMetrics.live_recovery_rate)}
                    </div>

                    <div className="kpi-meta">
                      Recovered amount / total live amount
                    </div>
                  </div>

                  <div className="live-recovery-kpi">
                    <div className="kpi-label">PAYMENTS FAILED</div>

                    <div className="live-recovery-kpi-value">
                      {liveMetrics.funnel?.payment_failed ?? 0}
                    </div>

                    <div className="kpi-meta">
                      Real Razorpay failure webhooks
                    </div>
                  </div>

                  <div className="live-recovery-kpi">
                    <div className="kpi-label">PENDING RECOVERY</div>

                    <div className="live-recovery-kpi-value warning-value">
                      {liveMetrics.pending_recovery ?? 0}
                    </div>

                    <div className="kpi-meta">
                      {formatCurrency(liveMetrics.pending_amount)} awaiting
                      recovery
                    </div>
                  </div>

                  <div className="live-recovery-kpi">
                    <div className="kpi-label">RECOVERED CASES</div>

                    <div className="live-recovery-kpi-value positive">
                      {liveMetrics.recovered_cases ?? 0}
                    </div>

                    <div className="kpi-meta">
                      {formatCurrency(liveMetrics.recovered_amount)} recovered
                    </div>
                  </div>
                </div>

                {/* LIVE MONEY POSITION */}

                <div className="section-card live-recovery-money-card">
                  <div className="section-heading">
                    <div>
                      <div className="section-kicker">
                        LIVE REVENUE POSITION
                      </div>

                      <h2>Recovery value</h2>
                    </div>

                    <span className="case-count">
                      {liveMetrics.live_cases ?? 0} live cases
                    </span>
                  </div>

                  <div className="live-recovery-money-grid">
                    <div>
                      <span>TOTAL FAILED AMOUNT</span>

                      <strong>
                        {formatCurrency(liveMetrics.total_amount)}
                      </strong>
                    </div>

                    <div>
                      <span>PENDING RECOVERY</span>

                      <strong className="warning-value">
                        {formatCurrency(liveMetrics.pending_amount)}
                      </strong>
                    </div>

                    <div>
                      <span>RECOVERED AMOUNT</span>

                      <strong className="positive">
                        {formatCurrency(liveMetrics.recovered_amount)}
                      </strong>
                    </div>

                    <div>
                      <span>RETRY LINKS</span>

                      <strong>{liveMetrics.retry_links ?? 0}</strong>
                    </div>
                  </div>
                </div>

                {/* LIVE RECOVERY FUNNEL */}

                <div className="section-card live-funnel-card">
                  <div className="section-heading">
                    <div>
                      <div className="section-kicker">
                        WEBHOOK-DRIVEN RECOVERY FUNNEL
                      </div>

                      <h2>From failure to recovery</h2>

                      <p className="section-description">
                        Every stage is derived from the live Razorpay case
                        store, not from the synthetic benchmark.
                      </p>
                    </div>
                  </div>

                  <div className="live-funnel">
                    <div className="live-funnel-stage failure">
                      <span className="live-funnel-number">
                        {liveMetrics.funnel?.payment_failed ?? 0}
                      </span>

                      <span className="live-funnel-label">PAYMENT FAILED</span>

                      <small>Razorpay webhook</small>
                    </div>

                    <div className="live-funnel-arrow">→</div>

                    <div className="live-funnel-stage pending">
                      <span className="live-funnel-number">
                        {liveMetrics.funnel?.recovery_pending ?? 0}
                      </span>

                      <span className="live-funnel-label">
                        RECOVERY PENDING
                      </span>

                      <small>Case awaiting action</small>
                    </div>

                    <div className="live-funnel-arrow">→</div>

                    <div className="live-funnel-stage retry">
                      <span className="live-funnel-number">
                        {liveMetrics.funnel?.retry_issued ?? 0}
                      </span>

                      <span className="live-funnel-label">RETRY ISSUED</span>

                      <small>Recovery link created</small>
                    </div>

                    <div className="live-funnel-arrow">→</div>

                    <div className="live-funnel-stage captured">
                      <span className="live-funnel-number">
                        {liveMetrics.funnel?.payment_captured ?? 0}
                      </span>

                      <span className="live-funnel-label">
                        PAYMENT CAPTURED
                      </span>

                      <small>Razorpay capture event</small>
                    </div>

                    <div className="live-funnel-arrow">→</div>

                    <div className="live-funnel-stage completed">
                      <span className="live-funnel-number">
                        {liveMetrics.funnel?.recovery_completed ?? 0}
                      </span>

                      <span className="live-funnel-label">
                        RECOVERY COMPLETED
                      </span>

                      <small>Verified recovery</small>
                    </div>
                  </div>
                </div>

                {/* SOURCE / INTEGRITY */}

                <div className="live-recovery-integrity">
                  <span className="live-integrity-dot" />

                  <div>
                    <strong>LIVE DATA SOURCE</strong>

                    <span>
                      {liveMetrics.source || "razorpay_live_case_store"}
                    </span>
                  </div>

                  <div className="live-integrity-divider" />

                  <div>
                    <strong>DATA MODE</strong>

                    <span>
                      {liveMetrics.is_live
                        ? "REAL-TIME OPERATIONAL"
                        : "OFFLINE"}
                    </span>
                  </div>

                  <div className="live-integrity-divider" />

                  <div>
                    <strong>BENCHMARK ISOLATION</strong>

                    <span>105-CASE SYNTHETIC DATASET UNAFFECTED</span>
                  </div>
                </div>
              </>
            ) : (
              <EmptyState>
                Live Razorpay recovery metrics are loading...
              </EmptyState>
            )}
          </section>

          {/* ====================================================
            PROMISE-TO-PAY TRACKER
        ==================================================== */}

          <section id="promises" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">HUMAN COMMITMENTS</div>
                <h2>Promise-to-pay tracker</h2>
                <p className="section-description">
                  Record a customer commitment with an exact deadline. Revive
                  automatically resolves the promise as paid when verified
                  payment arrives, or marks it broken when the deadline passes
                  without payment.
                </p>
              </div>

              {promiseMetrics && (
                <span className="live-badge">
                  {promiseMetrics.active_promises ?? 0} active
                </span>
              )}
            </div>

            {promiseMetrics && (
              <div className="promise-metrics-grid">
                <div className="promise-metric-card">
                  <span>TOTAL PROMISES</span>
                  <strong>{promiseMetrics.total_promises ?? 0}</strong>
                  <small>All current promise records</small>
                </div>

                <div className="promise-metric-card promise-metric-active">
                  <span>ACTIVE</span>
                  <strong>{promiseMetrics.active_promises ?? 0}</strong>
                  <small>Promises currently awaiting payment</small>
                </div>

                <div className="promise-metric-card">
                  <span>KEPT</span>
                  <strong>{promiseMetrics.promises_kept ?? 0}</strong>
                  <small>Customer commitments fulfilled</small>
                </div>

                <div className="promise-metric-card">
                  <span>BROKEN</span>
                  <strong>{promiseMetrics.promises_broken ?? 0}</strong>
                  <small>Commitments that expired or failed</small>
                </div>

                <div className="promise-metric-card">
                  <span>KEPT RATE</span>
                  <strong>
                    {formatPercent(promiseMetrics.promise_kept_rate)}
                  </strong>
                  <small>
                    {promiseMetrics.historical_promises ?? 0} historical records
                  </small>
                </div>
              </div>
            )}

            <div className="section-card promise-composer">
              <div className="promise-composer-header">
                <div>
                  <div className="section-kicker">CREATE COMMITMENT</div>
                  <h3>Record a new promise</h3>
                  <p>
                    Choose an existing recovery case and set the exact payment
                    deadline. Customer and invoice details are taken from the
                    authoritative case record.
                  </p>
                </div>
              </div>

              <div className="simulator-grid promise-form-grid-enhanced">
                <div className="simulator-field">
                  <label>CASE ID</label>

                  <input
                    list="promise-case-options"
                    type="text"
                    placeholder="RV-00042"
                    value={promiseForm.case_id}
                    onChange={(event) =>
                      handlePromiseCaseChange(event.target.value)
                    }
                  />

                  <datalist id="promise-case-options">
                    {promiseCaseOptions.map((item) => (
                      <option key={item.case_id} value={item.case_id}>
                        {item._live_payment_case ? "LIVE PAYMENT · " : ""}
                        {item.customer_name || item.customer_id || ""}
                      </option>
                    ))}
                  </datalist>

                  <small>
                    Includes unresolved live Razorpay payment cases and pipeline
                    cases.
                  </small>
                </div>

                <div className="simulator-field">
                  <label>CUSTOMER</label>

                  <input
                    type="text"
                    readOnly
                    value={
                      promiseCasePreview?.customer_name ||
                      promiseCasePreview?.customer_id ||
                      "Select a case"
                    }
                    placeholder="Auto-filled from case"
                  />

                  <small>
                    {promiseCasePreview?.customer_id
                      ? `Customer ID: ${promiseCasePreview.customer_id}`
                      : "Authoritative case metadata"}
                  </small>
                </div>

                <div className="simulator-field">
                  <label>INVOICE ID</label>

                  <input
                    type="text"
                    readOnly
                    value={promiseCasePreview?.invoice_id || ""}
                    placeholder="Auto-filled from case"
                  />

                  <small>
                    {promiseCasePreview?.invoice_id
                      ? "Linked to the recovery case"
                      : "No invoice ID available"}
                  </small>
                </div>

                <div className="simulator-field">
                  <label>OUTSTANDING AMOUNT (₹)</label>

                  <input
                    type="number"
                    readOnly
                    value={
                      promiseCasePreview
                        ? (getPromiseOutstandingAmount(promiseCasePreview) ??
                          "")
                        : ""
                    }
                    placeholder="Select a case"
                  />

                  <small>Maximum amount available for this promise.</small>
                </div>

                <div className="simulator-field">
                  <label>PROMISED AMOUNT (₹)</label>

                  <input
                    type="number"
                    min="1"
                    step="1"
                    max={
                      promiseCasePreview
                        ? (getPromiseOutstandingAmount(promiseCasePreview) ??
                          undefined)
                        : undefined
                    }
                    placeholder="Enter promised amount"
                    value={promiseForm.promised_amount}
                    onChange={(event) =>
                      updatePromiseFormField(
                        "promised_amount",
                        event.target.value,
                      )
                    }
                  />

                  <small>Cannot exceed the current outstanding amount.</small>
                </div>

                {/* PROMISE DEADLINE */}

                <div className="simulator-field">
                  <label>PROMISE DEADLINE</label>

                  <input
                    type="datetime-local"
                    value={promiseForm.promise_date}
                    min={formatLocalDateTimeInputValue(
                      new Date(Date.now() + 60000),
                    )}
                    max={formatLocalDateTimeInputValue(
                      new Date(Date.now() + 90 * 86400000),
                    )}
                    onChange={(event) =>
                      setPromiseForm((prev) => ({
                        ...prev,
                        promise_date: event.target.value,
                      }))
                    }
                  />

                  <small>
                    Exact date and time. Revive automatically marks the promise
                    broken if payment is not verified before this deadline.
                  </small>
                </div>

                <div className="promise-contact-card">
                  <div className="promise-contact-heading">
                    <div>
                      <label htmlFor="promise-customer-email">
                        CUSTOMER EMAIL
                      </label>
                      <small>Used for customer lifecycle notifications.</small>
                    </div>
                    <span className="promise-contact-status">EMAIL</span>
                  </div>

                  <div className="promise-email-input-wrap">
                    <span className="promise-email-icon">✉</span>
                    <input
                      id="promise-customer-email"
                      type="email"
                      value={promiseForm.customer_email}
                      onChange={(e) =>
                        updatePromiseFormField("customer_email", e.target.value)
                      }
                      placeholder="customer@example.com"
                      autoComplete="email"
                    />
                  </div>

                  <div className="promise-contact-footer">
                    <span>Customer notifications</span>
                    <span>
                      Promise created · Payment verified · Promise broken
                    </span>
                  </div>
                </div>
              </div>

              {promiseCasePreview && (
                <div className="promise-case-preview">
                  <div>
                    <span>CURRENT OUTCOME</span>
                    <strong>
                      {promiseCasePreview.outcome ||
                        promiseCasePreview.recovery_status ||
                        "—"}
                    </strong>
                  </div>

                  <div>
                    <span>RECOVERY STATUS</span>
                    <strong>{promiseCasePreview.recovery_status || "—"}</strong>
                  </div>

                  <div>
                    <span>CASE AMOUNT</span>
                    <strong>{formatCurrency(promiseCasePreview.amount)}</strong>
                  </div>
                </div>
              )}

              <div className="simulator-actions">
                <button
                  type="button"
                  className="run-button"
                  onClick={createPromise}
                  disabled={creatingPromise}
                >
                  {creatingPromise ? "Recording..." : "✓ Record promise"}
                </button>
              </div>

              {promiseError && (
                <div className="error-banner simulator-error">
                  ⚠ {promiseError}
                </div>
              )}
            </div>

            {promises.length > 0 ? (
              <div className="table-card promise-table-card">
                <div className="promise-table-header">
                  <div>
                    <div className="section-kicker">PROMISE REGISTER</div>
                    <h3>Active and current commitments</h3>
                  </div>

                  <span className="case-count">
                    {promises.length} record{promises.length === 1 ? "" : "s"}
                  </span>
                </div>

                <div className="table-wrapper">
                  <table className="a2a-table promise-table">
                    <thead>
                      <tr>
                        <th>CASE / CUSTOMER</th>
                        <th>INVOICE</th>
                        <th>PROMISED</th>
                        <th>PROMISE DATE</th>
                        <th>STATUS</th>
                        <th>PAYMENT PROOF</th>
                        <th>ACTIONS</th>
                      </tr>
                    </thead>

                    <tbody>
                      {promises.map((promise) => {
                        const normalizedStatus = String(
                          promise.status || "",
                        ).toLowerCase();
                        const verified = promise.payment_verified === true;
                        const manuallyFulfilled =
                          normalizedStatus === "paid" && !verified;

                        return (
                          <tr
                            key={
                              promise.promise_id ||
                              `${promise.case_id}-${promise.promise_date}`
                            }
                          >
                            <td>
                              <strong>{promise.case_id}</strong>
                              <small>
                                {promise.customer_name ||
                                  promise.customer_id ||
                                  "Customer unavailable"}
                              </small>
                            </td>

                            <td>
                              <strong>{promise.invoice_id || "—"}</strong>
                              <small>
                                Outstanding:{" "}
                                {formatCurrency(promise.outstanding_amount)}
                              </small>
                            </td>

                            <td>
                              <strong>
                                {formatCurrency(promise.promised_amount)}
                              </strong>
                              {promise.original_amount != null && (
                                <small>
                                  Original:{" "}
                                  {formatCurrency(promise.original_amount)}
                                </small>
                              )}
                            </td>

                            <td>{formatDate(promise.promise_date)}</td>

                            <td>
                              <StatusBadge value={promise.status} />
                            </td>

                            <td>
                              {verified ? (
                                <span className="promise-proof-badge verified">
                                  ✓ VERIFIED
                                </span>
                              ) : manuallyFulfilled ? (
                                <span className="promise-proof-badge manual">
                                  MANUAL / UNVERIFIED
                                </span>
                              ) : (
                                <span className="promise-proof-badge pending">
                                  NOT VERIFIED
                                </span>
                              )}

                              {promise.payment_reference && (
                                <small>{promise.payment_reference}</small>
                              )}

                              {promise.payment_link_url &&
                                normalizedStatus === "promised" && (
                                  <small>
                                    <a
                                      href={promise.payment_link_url}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="promise-payment-link"
                                    >
                                      Open payment link ↗
                                    </a>
                                  </small>
                                )}
                            </td>

                            <td>
                              <div className="promise-row-actions">
                                <button
                                  type="button"
                                  className="promise-history-button"
                                  onClick={() =>
                                    openPromiseHistory(promise.case_id)
                                  }
                                >
                                  History
                                </button>

                                {normalizedStatus === "promised" ? (
                                  <>
                                    {promise.payment_link_url ? (
                                      <button
                                        type="button"
                                        className="promise-paid-button"
                                        disabled={
                                          promiseActionId === promise.case_id
                                        }
                                        onClick={() =>
                                          createPromisePaymentLink(
                                            promise.case_id,
                                          )
                                        }
                                      >
                                        {promiseActionId === promise.case_id
                                          ? "Opening..."
                                          : "Pay via Razorpay"}
                                      </button>
                                    ) : (
                                      <button
                                        type="button"
                                        className="promise-paid-button"
                                        disabled={
                                          promiseActionId === promise.case_id
                                        }
                                        onClick={() =>
                                          createPromisePaymentLink(
                                            promise.case_id,
                                          )
                                        }
                                      >
                                        {promiseActionId === promise.case_id
                                          ? "Creating..."
                                          : "Create payment link"}
                                      </button>
                                    )}
                                  </>
                                ) : null}
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <div className="promise-table-footer">
                  <span>
                    Manual fulfillment changes the promise state but does not
                    create authoritative payment evidence.
                  </span>
                  <strong>
                    Verified payment requires payment evidence from the payment
                    integration/webhook path.
                  </strong>
                </div>
              </div>
            ) : (
              <EmptyState>
                No promises recorded yet. Select a case above to create the
                first customer commitment.
              </EmptyState>
            )}
          </section>

          {/* ====================================================
            LEDGER
        ==================================================== */}

          <section id="ledger" className="section-block">
            <div className="section-heading">
              <div>
                <div className="section-kicker">PROOF LAYER</div>

                <h2>Recovery ledger</h2>

                <p className="section-description">
                  A chronological, auditable record of every decision, attempt,
                  and outcome.
                </p>
              </div>

              <div className="case-count">{ledger.length} events</div>
            </div>

            {ledger.length > 0 ? (
              <div className="ledger-list">
                {paginatedLedger.map(({ event, globalIndex }) => {
                  const attemptNumber = getLedgerAttempt(
                    event,
                    globalIndex,
                    ledger,
                  );

                  const decision = getLedgerDecision(event);

                  const outcome = getLedgerOutcome(event);

                  const probability = getLedgerProbability(event);

                  const expectedRecovery = getLedgerExpectedRecovery(event);

                  const expectedValue = getLedgerExpectedValue(event);

                  const actionCost = getLedgerActionCost(event);

                  const recoveredAmount = getLedgerRecoveredAmount(event);

                  const uniqueKey =
                    event.event_id ||
                    event.ledger_event_id ||
                    `${event.case_id || "event"}-${globalIndex}`;

                  const policyBlocked =
                    event.policy_allowed === false ||
                    (Array.isArray(event.policy_blocking_reasons) &&
                      event.policy_blocking_reasons.length > 0);

                  return (
                    <div className="ledger-event" key={uniqueKey}>
                      <div className="ledger-marker">{globalIndex + 1}</div>

                      <div className="ledger-content">
                        <div className="ledger-top">
                          <div className="ledger-identity">
                            <strong>{event.case_id || "Recovery Event"}</strong>

                            <span className="ledger-attempt">
                              ATTEMPT #{attemptNumber}
                            </span>
                          </div>

                          <div className="ledger-statuses">
                            {decision && <StatusBadge value={decision} />}

                            {outcome && <StatusBadge value={outcome} />}

                            {policyBlocked && (
                              <StatusBadge value="POLICY BLOCKED" />
                            )}
                          </div>
                        </div>

                        <div className="ledger-details">
                          {event.action && (
                            <span>
                              Action: <strong>{event.action}</strong>
                            </span>
                          )}

                          {event.amount !== undefined && (
                            <span>
                              Amount:{" "}
                              <strong>{formatCurrency(event.amount)}</strong>
                            </span>
                          )}

                          {event.channel && (
                            <span>
                              Channel: <strong>{event.channel}</strong>
                            </span>
                          )}
                        </div>

                        {(probability !== null ||
                          expectedRecovery !== null ||
                          expectedValue !== null ||
                          actionCost !== null ||
                          recoveredAmount !== null) && (
                          <div className="ledger-metrics">
                            {probability !== null && (
                              <div>
                                <span>P(SUCCESS)</span>

                                <strong>{formatPercent(probability)}</strong>
                              </div>
                            )}

                            {expectedRecovery !== null && (
                              <div>
                                <span>EXPECTED RECOVERY</span>

                                <strong>
                                  {formatCurrency(expectedRecovery)}
                                </strong>
                              </div>
                            )}

                            {actionCost !== null && (
                              <div>
                                <span>ACTION COST</span>

                                <strong>{formatCurrency(actionCost)}</strong>
                              </div>
                            )}

                            {expectedValue !== null && (
                              <div>
                                <span>EXPECTED VALUE</span>

                                <strong>{formatCurrency(expectedValue)}</strong>
                              </div>
                            )}

                            {recoveredAmount !== null && (
                              <div>
                                <span>RECOVERED</span>

                                <strong>
                                  {formatCurrency(recoveredAmount)}
                                </strong>
                              </div>
                            )}
                          </div>
                        )}

                        <div className="ledger-audit">
                          {event.event_id && (
                            <span>
                              Event ID: <strong>{event.event_id}</strong>
                            </span>
                          )}

                          {event.status && (
                            <span>
                              Status: <strong>{event.status}</strong>
                            </span>
                          )}

                          {event.policy_allowed !== undefined && (
                            <span>
                              Policy:{" "}
                              <strong>
                                {event.policy_allowed ? "ALLOWED" : "BLOCKED"}
                              </strong>
                            </span>
                          )}
                        </div>

                        {policyBlocked &&
                          Array.isArray(event.policy_blocking_reasons) &&
                          event.policy_blocking_reasons.length > 0 && (
                            <div className="policy-warning">
                              <strong>Policy Blocking Reason</strong>

                              <ul>
                                {event.policy_blocking_reasons.map(
                                  (reason, reasonIndex) => (
                                    <li key={reasonIndex}>{reason}</li>
                                  ),
                                )}
                              </ul>
                            </div>
                          )}

                        {recoveredAmount !== null && (
                          <div className="ledger-recovery">
                            <span>RECOVERED AMOUNT</span>

                            <strong>{formatCurrency(recoveredAmount)}</strong>
                          </div>
                        )}

                        {event.timestamp && (
                          <small className="ledger-time">
                            {formatDate(event.timestamp)}
                          </small>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <EmptyState>
                No recovery ledger events have been recorded for this batch.
              </EmptyState>
            )}

            <Pagination
              page={ledgerPage}
              totalPages={totalLedgerPages}
              onChange={setLedgerPage}
              totalItems={ledger.length}
              pageSize={PAGE_SIZE}
            />
          </section>
        </main>

        <footer className="app-footer">
          <div className="app-footer-top">
            <div className="app-footer-brand">
              <span className="app-footer-mark">R</span>
              <div>
                <strong>REVIVE</strong>
                <span>AI REVENUE RECOVERY SYSTEM</span>
              </div>
            </div>

            <div className="app-footer-links">
              <span>Revenue Intelligence</span>
              <span className="app-footer-separator">•</span>
              <span>Operator Control Center</span>
              <span className="app-footer-separator">•</span>
              <span>v1.1</span>
            </div>

            <div className="app-footer-actions">
              <button
                type="button"
                className="app-footer-contact"
                onClick={() => setShowContactAdmin(true)}
              >
                <span className="app-footer-contact-icon" aria-hidden="true">
                  ✉
                </span>
                Contact admin
              </button>
            </div>
          </div>

          <div className="app-footer-bottom">
            <span>
              © {new Date().getFullYear()} Revive. All rights reserved.
            </span>

            <div className="app-footer-bottom-links">
              <button
                type="button"
                className="app-footer-bottom-link-button"
                onClick={() => setShowContactAdmin(true)}
              >
                admin@revive.ai
              </button>
              <span className="app-footer-separator">•</span>
              <span>Privacy</span>
              <span className="app-footer-separator">•</span>
              <span>Terms</span>
            </div>
          </div>
        </footer>
      </div>

      {/* ======================================================
          CONTACT ADMIN MODAL
      ====================================================== */}

      <ContactAdminModal
        open={showContactAdmin}
        onClose={() => setShowContactAdmin(false)}
        user={user}
      />

      {/* ======================================================
          PROMISE HISTORY MODAL
      ====================================================== */}

      {(promiseHistoryLoading || promiseHistory) && (
        <div className="modal-backdrop" onClick={closePromiseHistory}>
          <div
            className="modal promise-history-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <div className="section-kicker">PROMISE AUDIT</div>
                <h2>{promiseHistory?.case_id || "Promise history"}</h2>
                <p className="modal-subtitle">
                  Full promise lifecycle, payment evidence, and audit
                  transitions
                </p>
              </div>

              <button
                type="button"
                className="close-button"
                onClick={closePromiseHistory}
              >
                ×
              </button>
            </div>

            {promiseHistoryLoading ? (
              <div className="promise-history-loading">
                Loading promise history...
              </div>
            ) : (
              <>
                <div className="promise-history-summary">
                  <div>
                    <span>CURRENT STATUS</span>
                    <StatusBadge value={promiseHistory?.current?.status} />
                  </div>

                  <div>
                    <span>PROMISE ID</span>
                    <strong className="mono">
                      {promiseHistory?.current?.promise_id || "—"}
                    </strong>
                  </div>

                  <div>
                    <span>PROMISED AMOUNT</span>
                    <strong>
                      {formatCurrency(promiseHistory?.current?.promised_amount)}
                    </strong>
                  </div>

                  <div>
                    <span>PROMISE DATE</span>
                    <strong>
                      {formatDate(promiseHistory?.current?.promise_date)}
                    </strong>
                  </div>

                  <div>
                    <span>PAYMENT PROOF</span>
                    <strong>
                      {promiseHistory?.current?.payment_verified
                        ? "VERIFIED"
                        : "NOT VERIFIED"}
                    </strong>
                  </div>

                  <div>
                    <span>PAYMENT SOURCE</span>
                    <strong>
                      {promiseHistory?.current?.payment_source || "—"}
                    </strong>
                  </div>
                </div>

                <div className="detail-grid promise-detail-grid">
                  <div>
                    <span>CUSTOMER</span>
                    <strong>
                      {promiseHistory?.current?.customer_name ||
                        promiseHistory?.current?.customer_id ||
                        "—"}
                    </strong>
                  </div>

                  <div>
                    <span>INVOICE</span>
                    <strong>
                      {promiseHistory?.current?.invoice_id || "—"}
                    </strong>
                  </div>

                  <div>
                    <span>OUTSTANDING</span>
                    <strong>
                      {formatCurrency(
                        promiseHistory?.current?.outstanding_amount,
                      )}
                    </strong>
                  </div>

                  <div>
                    <span>PAYMENT REFERENCE</span>
                    <strong>
                      {promiseHistory?.current?.payment_reference || "—"}
                    </strong>
                  </div>
                </div>

                <div className="promise-history-section">
                  <div className="section-kicker">PROMISE HISTORY</div>

                  {Array.isArray(promiseHistory?.history) &&
                  promiseHistory.history.length > 0 ? (
                    <div className="promise-history-list">
                      {promiseHistory.history.map((item, index) => (
                        <div
                          className="promise-history-item"
                          key={item.promise_id || `${item.case_id}-${index}`}
                        >
                          <div>
                            <strong>
                              {item.promise_id || "Promise record"}
                            </strong>
                            <span>
                              {formatDate(item.promise_date)} ·{" "}
                              {formatCurrency(item.promised_amount)}
                            </span>
                          </div>

                          <StatusBadge value={item.status} />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="empty-card">
                      No historical promise records are available.
                    </div>
                  )}
                </div>

                <div className="promise-history-section">
                  <div className="section-kicker">AUDIT TRAIL</div>

                  {Array.isArray(promiseHistory?.audit_trail) &&
                  promiseHistory.audit_trail.length > 0 ? (
                    <div className="promise-audit-list">
                      {promiseHistory.audit_trail.map((transition, index) => (
                        <div
                          className="promise-audit-item"
                          key={`${transition.timestamp || index}-${index}`}
                        >
                          <div className="promise-audit-copy">
                            <strong>
                              {transition.reason || "Promise state changed"}
                            </strong>
                            <small>
                              {formatDate(transition.timestamp)}
                              {transition.promise_id
                                ? ` · ${transition.promise_id}`
                                : ""}
                            </small>
                          </div>

                          <div className="promise-audit-transition">
                            <StatusBadge
                              value={transition.from_status || "none"}
                            />
                            <span>→</span>
                            <StatusBadge value={transition.to_status} />
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="empty-card">
                      No audit transitions are available.
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ======================================================
          CASE DETAIL MODAL
      ====================================================== */}

      {selectedCase && (
        <div className="modal-backdrop" onClick={closeCaseModal}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <div>
                <div className="section-kicker">CASE DETAILS</div>

                <h2>{selectedCase.case_id}</h2>

                <p className="modal-subtitle">AI recovery decision evidence</p>
              </div>

              <button className="close-button" onClick={closeCaseModal}>
                ×
              </button>
            </div>

            <div className="detail-grid">
              <div>
                <span>CUSTOMER</span>
                <strong>{selectedCase.customer_id}</strong>
              </div>

              <div>
                <span>AMOUNT</span>
                <strong>{formatCurrency(selectedCase.amount)}</strong>
              </div>

              <div>
                <span>SURFACE</span>
                <strong>{selectedCase.surface}</strong>
              </div>

              <div>
                <span>ROOT CAUSE</span>
                <strong>{selectedCase.root_cause}</strong>
              </div>

              <div>
                <span>RECOMMENDED ACTION</span>
                <strong>{selectedCase.action}</strong>
              </div>

              <div>
                <span>CHANNEL</span>
                <strong>{selectedCase.channel}</strong>
              </div>

              <div>
                <span>SUCCESS PROBABILITY</span>

                <strong>{formatPercent(selectedCase.roi_probability)}</strong>
              </div>

              <div>
                <span>EXPECTED RECOVERY</span>

                <strong>
                  {formatCurrency(selectedCase.expected_recovery)}
                </strong>
              </div>

              <div>
                <span>EXPECTED VALUE</span>

                <strong>{formatCurrency(selectedCase.expected_value)}</strong>
              </div>

              <div>
                <span>ACTION COST</span>

                <strong>{formatCurrency(selectedCase.action_cost)}</strong>
              </div>

              <div>
                <span>POLICY</span>

                <StatusBadge
                  value={selectedCase.policy_allowed ? "ALLOWED" : "BLOCKED"}
                />
              </div>

              <div>
                <span>ROI DECISION</span>

                <StatusBadge value={selectedCase.roi_decision} />
              </div>

              <div>
                <span>OUTCOME</span>

                <StatusBadge value={selectedCase.outcome} />
              </div>

              <div>
                <span>RECOVERED AMOUNT</span>

                <strong>{formatCurrency(selectedCase.recovered_amount)}</strong>
              </div>

              <div>
                <span>A2A ELIGIBLE</span>

                <strong>{selectedCase.a2a_eligible ? "YES" : "NO"}</strong>
              </div>

              <div>
                <span>A2A OUTCOME</span>

                <StatusBadge value={selectedCase.a2a_outcome || "N/A"} />
              </div>
            </div>

            {!selectedCase.policy_allowed &&
              Array.isArray(selectedCase.policy_blocking_reasons) &&
              selectedCase.policy_blocking_reasons.length > 0 && (
                <div className="policy-warning">
                  <strong>Policy Blocking Reasons</strong>

                  <ul>
                    {selectedCase.policy_blocking_reasons.map(
                      (reason, index) => (
                        <li key={index}>{reason}</li>
                      ),
                    )}
                  </ul>
                </div>
              )}

            {selectedCase.mandate_retry_plan && (
              <div className="mandate-retry-panel">
                <div className="mandate-retry-header">
                  <span>MANDATE RETRY SCHEDULE</span>

                  <StatusBadge
                    value={
                      selectedCase.mandate_retry_plan.escalate
                        ? "ESCALATE"
                        : "SCHEDULED"
                    }
                  />
                </div>

                <p className="mandate-retry-intro">
                  This mandate is still active — only the debit attempt failed.
                  Compliant retries follow UPI Autopay / eNACH rules: a
                  pre-debit notice before every attempt, within the current
                  billing cycle (
                  {formatDate(selectedCase.mandate_retry_plan.cycle_started_at)}
                  {" – "}
                  {formatDate(selectedCase.mandate_retry_plan.cycle_ends_at)}
                  ).
                </p>

                {selectedCase.mandate_retry_plan.attempts.length > 0 && (
                  <table className="mandate-retry-table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>Notice sent</th>
                        <th>Retry attempt</th>
                      </tr>
                    </thead>

                    <tbody>
                      {selectedCase.mandate_retry_plan.attempts.map(
                        (attempt) => (
                          <tr key={attempt.attempt_number}>
                            <td>{attempt.attempt_number}</td>
                            <td>{formatDate(attempt.notify_at)}</td>
                            <td>{formatDate(attempt.retry_at)}</td>
                          </tr>
                        ),
                      )}
                    </tbody>
                  </table>
                )}

                {selectedCase.mandate_retry_plan.escalate && (
                  <div className="mandate-retry-escalation">
                    <strong>Per-cycle attempt cap reached.</strong> No further
                    debit attempt is compliant this cycle — escalating to{" "}
                    <code>
                      {selectedCase.mandate_retry_plan.escalation_action}
                    </code>
                    .
                  </div>
                )}

                <details className="mandate-retry-audit">
                  <summary>Audit trail</summary>

                  <ul>
                    {selectedCase.mandate_retry_plan.audit_trail.map(
                      (line, index) => (
                        <li key={index}>{line}</li>
                      ),
                    )}
                  </ul>
                </details>
              </div>
            )}

            <div className="settlement-reason decision-explainer-box">
              <span>DECISION EXPLAINER</span>

              <p>
                Ask REVIVE why this decision was made. The explanation is
                grounded in the actual pipeline and recovery ledger evidence.
              </p>

              <input
                type="text"
                value={explanationQuestion}
                onChange={(event) => setExplanationQuestion(event.target.value)}
                placeholder={
                  selectedCase.roi_decision === "STOP"
                    ? "Why was this case stopped?"
                    : "Why did REVIVE make this decision?"
                }
                className="explanation-input"
              />

              <button
                className="run-button"
                onClick={handleExplainSelectedCase}
                disabled={explainingCase}
              >
                {explainingCase ? "Explaining..." : "✦ Explain Decision"}
              </button>
            </div>

            <VoiceScriptPanel
              caseId={selectedCase.case_id}
              apiBase={API_BASE}
            />
          </div>
        </div>
      )}

      {/* ======================================================
          LIVE PAYMENT DETAIL MODAL
      ====================================================== */}

      {selectedLivePayment && (
        <div className="modal-backdrop" onClick={closeLivePaymentModal}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <div>
                <div className="section-kicker">LIVE PAYMENT FAILURE</div>

                <h2>{selectedLivePayment.case_id}</h2>

                <p className="modal-subtitle">
                  {selectedLivePayment.invoice_id
                    ? `Invoice ${selectedLivePayment.invoice_id}`
                    : "Real Razorpay test-mode failure"}
                </p>
              </div>

              <button
                className="close-button"
                onClick={closeLivePaymentModal}
              >
                ×
              </button>
            </div>

            <div className="detail-grid">
              <div>
                <span>CUSTOMER</span>
                <strong>{selectedLivePayment.customer_name || "—"}</strong>
              </div>

              <div>
                <span>EMAIL</span>
                <strong>{selectedLivePayment.customer_email || "—"}</strong>
              </div>

              <div>
                <span>AMOUNT</span>
                <strong>{formatCurrency(selectedLivePayment.amount)}</strong>
              </div>

              <div>
                <span>SURFACE</span>
                <strong>{selectedLivePayment.surface || "—"}</strong>
              </div>

              <div>
                <span>INVOICE ID</span>
                <strong>{selectedLivePayment.invoice_id || "—"}</strong>
              </div>

              <div>
                <span>HAS AP AGENT</span>
                <strong>
                  {selectedLivePayment.has_ap_agent ? "YES" : "NO"}
                </strong>
              </div>

              <div>
                <span>DISPUTED</span>
                <strong>{selectedLivePayment.disputed ? "YES" : "NO"}</strong>
              </div>

              <div>
                <span>ROOT CAUSE</span>

                <StatusBadge
                  value={selectedLivePayment.root_cause_label || "N/A"}
                />
              </div>

              <div>
                <span>PAYMENT METHOD</span>
                <strong>{selectedLivePayment.payment_method || "—"}</strong>
              </div>

              <div>
                <span>BANK / WALLET</span>
                <strong>{selectedLivePayment.bank || "—"}</strong>
              </div>

              <div>
                <span>CARD NETWORK</span>
                <strong>{selectedLivePayment.card_network || "—"}</strong>
              </div>

              <div>
                <span>STATUS</span>

                <StatusBadge
                  value={
                    selectedLivePayment.recovery_status ||
                    selectedLivePayment.outcome ||
                    "PENDING_RECOVERY"
                  }
                />
              </div>

              <div>
                <span>RECOVERED AMOUNT</span>

                <strong>
                  {formatCurrency(selectedLivePayment.recovered_amount)}
                </strong>
              </div>

              <div>
                <span>RECOVERED AT</span>

                <strong>
                  {selectedLivePayment.recovered_at
                    ? formatDate(selectedLivePayment.recovered_at)
                    : "—"}
                </strong>
              </div>

              <div>
                <span>RAZORPAY PAYMENT ID</span>

                <strong>
                  {selectedLivePayment.razorpay_payment_id || "—"}
                </strong>
              </div>

              <div>
                <span>DETECTED AT</span>
                <strong>{formatDate(selectedLivePayment.timestamp)}</strong>
              </div>
            </div>

            {selectedLivePayment.razorpay_raw_error &&
              (selectedLivePayment.razorpay_raw_error.error_description ||
                selectedLivePayment.razorpay_raw_error.error_reason) && (
                <div className="policy-warning">
                  <strong>Razorpay Failure Detail</strong>

                  <ul>
                    {selectedLivePayment.razorpay_raw_error.error_reason && (
                      <li>
                        Reason:{" "}
                        {selectedLivePayment.razorpay_raw_error.error_reason}
                      </li>
                    )}

                    {selectedLivePayment.razorpay_raw_error
                      .error_description && (
                      <li>
                        {
                          selectedLivePayment.razorpay_raw_error
                            .error_description
                        }
                      </li>
                    )}

                    {selectedLivePayment.razorpay_raw_error.error_code && (
                      <li>
                        Code:{" "}
                        {selectedLivePayment.razorpay_raw_error.error_code}
                      </li>
                    )}
                  </ul>
                </div>
              )}

            <div className="modal-footer-actions">
              {selectedLivePayment.recovery_status ===
                "PENDING_RECOVERY" && (
                <button
                  className="run-button"
                  onClick={() =>
                    retryLivePayment(selectedLivePayment.case_id)
                  }
                  disabled={retryingCaseId === selectedLivePayment.case_id}
                >
                  {retryingCaseId === selectedLivePayment.case_id
                    ? "Opening..."
                    : "↻ Retry Payment"}
                </button>
              )}

              {isLiveA2aEligible(selectedLivePayment) && (
                <button
                  className="close-button reset-button"
                  onClick={() => {
                    const existing = getLiveA2aForCase(
                      selectedLivePayment.case_id,
                    );

                    closeLivePaymentModal();

                    if (existing) {
                      setSelectedLiveA2a(existing);
                    } else {
                      startLiveA2aSettlement(selectedLivePayment);
                    }
                  }}
                  disabled={a2aActionCaseId === selectedLivePayment.case_id}
                >
                  {getLiveA2aForCase(selectedLivePayment.case_id)
                    ? "View A2A Agreement"
                    : "⚡ Start A2A Settlement"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ======================================================
          DECISION EXPLANATION MODAL
      ====================================================== */}

      {explanation && (
        <div className="modal-backdrop" onClick={closeExplanationModal}>
          <div
            className="modal settlement-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <div className="section-kicker">DECISION EXPLAINER</div>

                <h2>
                  {explanationCase?.case_id ||
                    explanation.case_id ||
                    "Case Explanation"}
                </h2>

                <p className="modal-subtitle">
                  Grounded in Revive pipeline evidence and recovery ledger
                  history
                </p>
              </div>

              <button className="close-button" onClick={closeExplanationModal}>
                ×
              </button>
            </div>

            {explanationBody?.summary && (
              <div className="roi-summary-line">{explanationBody.summary}</div>
            )}

            <div
              className="settlement-summary"
              style={{
                marginTop: "16px",
              }}
            >
              <div>
                <span>DECISION</span>

                <span
                  className={explanationDecisionClass(
                    explanationBody?.decision,
                  )}
                >
                  {explanationBody?.decision || "UNKNOWN"}
                </span>
              </div>

              <div>
                <span>OUTCOME</span>

                <StatusBadge value={explanationBody?.outcome || "UNKNOWN"} />
              </div>

              <div>
                <span>POLICY</span>

                <StatusBadge
                  value={
                    explanationBody?.policy_allowed ? "ALLOWED" : "BLOCKED"
                  }
                />
              </div>

              <div>
                <span>ATTEMPT</span>

                <strong>
                  {explanationBody?.attempt_number ??
                    explanationDecision?.roi_attempt_number ??
                    "—"}
                </strong>
              </div>
            </div>

            {explanationPolicy && (
              <div className="transcript-section">
                <div className="transcript-title">Policy Evidence</div>

                <div className="policy-evidence-list">
                  <div className="policy-evidence-item policy-result">
                    <strong
                      className={
                        explanationPolicy.allowed
                          ? "check-passed"
                          : "check-failed"
                      }
                    >
                      {explanationPolicy.allowed
                        ? "✓ POLICY ALLOWED"
                        : "✕ POLICY BLOCKED"}
                    </strong>
                  </div>

                  {Array.isArray(explanationPolicy.blocking_reasons) &&
                    explanationPolicy.blocking_reasons.length > 0 && (
                      <div className="policy-evidence-item">
                        <strong className="check-failed">
                          Blocking Reasons
                        </strong>

                        <ul>
                          {explanationPolicy.blocking_reasons.map(
                            (reason, index) => (
                              <li key={index}>{reason}</li>
                            ),
                          )}
                        </ul>
                      </div>
                    )}
                </div>
              </div>
            )}

            {Array.isArray(explanationBody?.reasons) &&
              explanationBody.reasons.length > 0 && (
                <div className="transcript-section">
                  <div className="transcript-title">Decision Reasons</div>

                  <div className="policy-evidence-list">
                    {explanationBody.reasons.map((reason, index) => (
                      <div className="policy-evidence-item" key={index}>
                        {reason}
                      </div>
                    ))}
                  </div>
                </div>
              )}

            <div className="transcript-section">
              <div className="transcript-title">Financial Impact</div>

              <div className="settlement-summary">
                <div>
                  <span>AMOUNT AT RISK</span>

                  <strong>
                    {formatCurrency(
                      explanationBody?.financial_impact?.amount_at_risk ??
                        explanationCase?.amount,
                    )}
                  </strong>
                </div>

                <div>
                  <span>EXPECTED RECOVERY</span>

                  <strong>
                    {formatCurrency(
                      explanationRoi?.expected_recovery ??
                        explanationBody?.financial_impact?.expected_recovery,
                    )}
                  </strong>
                </div>

                <div>
                  <span>ACTION COST</span>

                  <strong>
                    {formatCurrency(
                      explanationRoi?.action_cost ??
                        explanationBody?.financial_impact?.action_cost,
                    )}
                  </strong>
                </div>

                <div>
                  <span>EXPECTED VALUE</span>

                  <strong>
                    {formatCurrency(
                      explanationRoi?.expected_value ??
                        explanationBody?.financial_impact?.expected_value,
                    )}
                  </strong>
                </div>
              </div>

              <div
                className="settlement-summary"
                style={{
                  marginTop: "10px",
                }}
              >
                <div>
                  <span>P(SUCCESS)</span>

                  <strong>{formatPercent(explanationRoi?.probability)}</strong>
                </div>

                <div>
                  <span>RECOVERED AMOUNT</span>

                  <strong>
                    {formatCurrency(
                      explanationRecovery?.recovered_amount ??
                        explanationBody?.financial_impact?.recovered_amount,
                    )}
                  </strong>
                </div>

                <div>
                  <span>ROOT CAUSE</span>

                  <strong>{explanationCase?.root_cause || "—"}</strong>
                </div>

                <div>
                  <span>ACTION</span>

                  <strong>{explanationCase?.action || "—"}</strong>
                </div>
              </div>
            </div>

            {explanationBody?.financial_explanation && (
              <ExplanationSection title="FINANCIAL EXPLANATION">
                {explanationBody.financial_explanation}
              </ExplanationSection>
            )}

            {explanationBody?.policy_explanation && (
              <ExplanationSection title="POLICY EXPLANATION">
                {explanationBody.policy_explanation}
              </ExplanationSection>
            )}

            {explanationLedger && (
              <div className="transcript-section">
                <div className="transcript-title">Recovery Ledger Evidence</div>

                <div className="policy-evidence-list">
                  <div className="policy-evidence-item policy-result">
                    <strong>
                      {explanationLedger.event_count} ledger event
                      {explanationLedger.event_count === 1 ? "" : "s"} recorded
                    </strong>
                  </div>

                  {Array.isArray(explanationLedger.events) &&
                    explanationLedger.events.map((event, index) => (
                      <div
                        className="policy-evidence-item"
                        key={`${event.case_id}-${event.attempt_number}-${index}`}
                      >
                        <strong>
                          Attempt #{event.attempt_number} — {event.action}
                        </strong>

                        <div className="check-message">
                          Decision: {event.decision || "—"}
                          {" • "}
                          Outcome: {event.outcome || "—"}
                          {" • "}
                          Policy: {event.policy_allowed ? "ALLOWED" : "BLOCKED"}
                        </div>

                        {event.reason && (
                          <div className="check-message">{event.reason}</div>
                        )}
                      </div>
                    ))}
                </div>
              </div>
            )}

            {explanationBody?.audit_note && (
              <div className="evidence-box">
                <div className="evidence-title">Audit Note</div>

                <div className="evidence-item">
                  {explanationBody.audit_note}
                </div>
              </div>
            )}

            <div
              className="ledger-audit"
              style={{
                marginTop: "18px",
              }}
            >
              <span>
                Explanation mode:{" "}
                <strong>
                  {String(explanationData?.mode || "fallback").toUpperCase()}
                </strong>
              </span>
            </div>
          </div>
        </div>
      )}

      {/* ======================================================
          LIVE A2A DETAIL MODAL
      ====================================================== */}

      {selectedLiveA2a && (
        <div
          className="modal-backdrop"
          onClick={() => setSelectedLiveA2a(null)}
        >
          <div
            className="modal live-a2a-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <div className="section-kicker">LIVE A2A SETTLEMENT</div>
                <h2>{selectedLiveA2a.case_id}</h2>
                <p className="modal-subtitle">
                  {selectedLiveA2a.invoice_id || "Invoice unavailable"}
                </p>
              </div>
              <button
                className="close-button"
                onClick={() => setSelectedLiveA2a(null)}
              >
                ×
              </button>
            </div>

            <div
              className={`live-a2a-modal-hero ${selectedLiveA2a.recovery_confirmed ? "confirmed" : ""}`}
            >
              <div>
                <span>
                  {selectedLiveA2a.recovery_confirmed
                    ? "RECOVERY CONFIRMED"
                    : "AGREEMENT STATUS"}
                </span>
                <strong>
                  {selectedLiveA2a.recovery_confirmed
                    ? "✓ Payment confirmed"
                    : "Agreement reached"}
                </strong>
              </div>
              <div>
                <span>AGREED AMOUNT</span>
                <strong>{formatCurrency(selectedLiveA2a.agreed_amount)}</strong>
              </div>
            </div>

            <div className="live-a2a-timeline">
              {[
                ["Agent discovery", true],
                ["Negotiation", true],
                ["Agreement", true],
                ["Payment", selectedLiveA2a.recovery_confirmed],
                ["Recovery confirmed", selectedLiveA2a.recovery_confirmed],
              ].map(([label, done], index) => (
                <div className="live-a2a-timeline-row" key={label}>
                  <div
                    className={`live-a2a-timeline-dot ${done ? "done" : "waiting"}`}
                  >
                    {done ? "✓" : index + 1}
                  </div>
                  <div>
                    <strong>{label}</strong>
                    <span>
                      {done ? "Completed" : "Awaiting verified payment capture"}
                    </span>
                  </div>
                </div>
              ))}
            </div>

            <div className="settlement-summary live-a2a-detail-grid">
              <div>
                <span>SETTLEMENT STATUS</span>
                <StatusBadge
                  value={selectedLiveA2a.settlement_status || "AGREED"}
                />
              </div>
              <div>
                <span>PAYMENT STATUS</span>
                <StatusBadge
                  value={selectedLiveA2a.payment_status || "PENDING"}
                />
              </div>
              <div>
                <span>RECOVERY</span>
                <StatusBadge
                  value={
                    selectedLiveA2a.recovery_confirmed ? "CONFIRMED" : "PENDING"
                  }
                />
              </div>
              <div>
                <span>PAYER AGENT</span>
                <strong className="mono">
                  {selectedLiveA2a.payer_agent_id || "—"}
                </strong>
              </div>
            </div>

            <div className="live-a2a-proof">
              <div>
                <span>AGREEMENT ID</span>
                <strong className="mono">
                  {selectedLiveA2a.agreement_id || "—"}
                </strong>
              </div>
              <div>
                <span>RAZORPAY PAYMENT ID</span>
                <strong className="mono">
                  {selectedLiveA2a.razorpay_payment_id || "Awaiting capture"}
                </strong>
              </div>
              <div>
                <span>CONFIRMED AT</span>
                <strong>{formatDate(selectedLiveA2a.confirmed_at)}</strong>
              </div>
            </div>

            <div className="live-a2a-trust-note">
              <strong>Payment authority</strong>
              <p>
                Recovery is only confirmed after the verified Razorpay{" "}
                <span className="mono">payment.captured</span> webhook. Reaching
                an A2A agreement alone does not mark the invoice recovered.
              </p>
            </div>

            {selectedLiveA2a.payment_url &&
              !selectedLiveA2a.recovery_confirmed && (
                <button
                  type="button"
                  className="run-button"
                  onClick={() =>
                    window.open(
                      selectedLiveA2a.payment_url,
                      "_blank",
                      "noopener,noreferrer",
                    )
                  }
                >
                  Open payment link ↗
                </button>
              )}
          </div>
        </div>
      )}

      {/* ======================================================
          A2A DETAIL MODAL
      ====================================================== */}

      {selectedSettlement && (
        <div
          className="modal-backdrop"
          onClick={() => setSelectedSettlement(null)}
        >
          <div
            className="modal settlement-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <div className="section-kicker">A2A SETTLEMENT</div>

                <h2>{selectedSettlement.case_id}</h2>

                <p className="modal-subtitle">
                  {selectedSettlement.invoice_id}
                </p>
              </div>

              <button
                className="close-button"
                onClick={() => setSelectedSettlement(null)}
              >
                ×
              </button>
            </div>

            <div className="settlement-summary">
              <div>
                <span>OUTCOME</span>

                <StatusBadge value={selectedSettlement.outcome} />
              </div>

              <div>
                <span>FINAL AMOUNT</span>

                <strong>
                  {formatCurrency(selectedSettlement.final_amount)}
                </strong>
              </div>

              <div>
                <span>DISCOUNT</span>

                <strong>
                  {Number(selectedSettlement.discount_percent || 0).toFixed(2)}%
                </strong>
              </div>

              <div>
                <span>ROUNDS</span>

                <strong>{selectedSettlement.rounds}</strong>
              </div>
            </div>

            {selectedSettlement.reason && (
              <div className="settlement-reason">
                <span>SETTLEMENT REASON</span>

                <p>{selectedSettlement.reason}</p>
              </div>
            )}

            {selectedSettlement.policy_evidence && (
              <div className="transcript-section">
                <div className="transcript-title">Policy Evidence</div>

                <div className="policy-evidence-list">
                  <div className="policy-evidence-item policy-result">
                    <strong>
                      Policy:{" "}
                      {selectedSettlement.policy_evidence.allowed
                        ? "ALLOWED"
                        : "BLOCKED"}
                    </strong>
                  </div>

                  {Array.isArray(selectedSettlement.policy_evidence.checks) &&
                    selectedSettlement.policy_evidence.checks.length > 0 &&
                    selectedSettlement.policy_evidence.checks.map(
                      (check, index) => (
                        <div className="policy-evidence-item" key={index}>
                          <strong
                            className={
                              check.passed ? "check-passed" : "check-failed"
                            }
                          >
                            {check.passed ? "✓" : "✕"} {check.name}
                          </strong>

                          {check.message && (
                            <div className="check-message">{check.message}</div>
                          )}
                        </div>
                      ),
                    )}

                  {Array.isArray(
                    selectedSettlement.policy_evidence.blocking_reasons,
                  ) &&
                    selectedSettlement.policy_evidence.blocking_reasons.length >
                      0 && (
                      <div className="policy-evidence-item">
                        <strong className="check-failed">
                          Blocking Reasons
                        </strong>

                        <ul>
                          {selectedSettlement.policy_evidence.blocking_reasons.map(
                            (reason, index) => (
                              <li key={index}>{reason}</li>
                            ),
                          )}
                        </ul>
                      </div>
                    )}
                </div>
              </div>
            )}

            {Array.isArray(selectedSettlement.transcript) &&
              selectedSettlement.transcript.length > 0 && (
                <div className="transcript-section">
                  <div className="transcript-title">Negotiation Transcript</div>

                  <div className="transcript">
                    {selectedSettlement.transcript.map((round, index) => (
                      <div
                        className="transcript-round"
                        key={round.round_number || index}
                      >
                        <div className="round-number">
                          ROUND {round.round_number || index + 1}
                        </div>

                        <div className="round-grid">
                          <div>
                            <span>MERCHANT AMOUNT</span>

                            <strong>
                              {formatCurrency(round.merchant_amount)}
                            </strong>
                          </div>

                          <div>
                            <span>PAYER AMOUNT</span>

                            <strong>
                              {formatCurrency(round.payer_amount)}
                            </strong>
                          </div>

                          <div>
                            <span>DISCOUNT</span>

                            <strong>
                              {Number(round.discount_percent || 0).toFixed(2)}%
                            </strong>
                          </div>

                          <div>
                            <span>MERCHANT</span>

                            <StatusBadge
                              value={
                                round.merchant_status ||
                                round.merchant_decision ||
                                "—"
                              }
                            />
                          </div>

                          <div>
                            <span>PAYER</span>

                            <StatusBadge
                              value={
                                round.payer_status ||
                                round.payer_decision ||
                                "—"
                              }
                            />
                          </div>
                        </div>

                        {round.message && (
                          <div className="round-message">{round.message}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
          </div>
        </div>
      )}

      {/* ======================================================
          OPS TASK BOARD (KANBAN) MODAL
      ====================================================== */}

      {kanbanOpen && (
        <div className="modal-backdrop" onClick={() => setKanbanOpen(false)}>
          <div
            className="modal kanban-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <div className="section-kicker">OPERATOR WORKFLOW</div>

                <h2>Ops task board</h2>

                <p className="modal-subtitle">
                  Every item across Revive that still needs a human — pending
                  live payments, active promises, unconfirmed A2A agreements,
                  PSR alerts, and high-value pursue cases — collected into one
                  board. Drag a card (or use the arrows) as you triage it.
                </p>
              </div>

              <button
                type="button"
                className="close-button"
                onClick={() => setKanbanOpen(false)}
              >
                ×
              </button>
            </div>

            <KanbanBoard
              cases={cases}
              livePayments={livePayments}
              promises={promises}
              liveA2aSettlements={liveA2aSettlements}
              psrAlerts={psrAlerts}
            />
          </div>
        </div>
      )}

      {toast && (
        <div className="toast">
          <span>✓</span>
          <span>{toast}</span>
        </div>
      )}

      <CopilotWidget />
    </div>
  );
}

export default App;
