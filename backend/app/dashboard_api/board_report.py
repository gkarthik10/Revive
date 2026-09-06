"""
Revive - Board Report PDF

Generates the executive Revive Board Report as a downloadable PDF.

This module owns ONLY report rendering. It does not redefine or
recompute recovery business logic — every figure in the report is
read directly from the authoritative pipeline result dict produced
by app.pipeline.pipeline_to_dict().

Previously this lived inline inside dashboard_api/api.py and only
rendered summary counts for several sections (a single PSR alert,
an aggregate ledger-event count with no rows, no case list, no
settlement list). "Export snapshot" on the dashboard is supposed to
be a complete, self-contained artifact a board member can read
without the dashboard open, so every section below now renders the
full underlying data instead of a headline number:

    - ALL PSR Guardian alerts (not just the first one)
    - The full Case Register (every case, not just aggregate counts)
    - The full A2A Settlement Register (every settlement row)
    - The full Recovery Ledger (every event, not just a count)

That fix covered the synthetic 105-case benchmark pipeline result,
but the dashboard also shows real Razorpay Test Mode data that is
intentionally kept OUT of that synthetic pipeline (see
dashboard_api/api.py: cases vs live-cases, metrics vs live_metrics,
psr_alerts vs live psr alerts, a2a vs live A2A settlements, plus the
separate real Razorpay sandbox capture). Because build_board_report_pdf
only ever received the synthetic result, "Export snapshot" still
silently omitted all of that -- every real Razorpay Test Mode case,
alert, and settlement. build_board_report_pdf now optionally accepts
a `live_data` dict (see its docstring) and renders a "LIVE RAZORPAY
TEST MODE" section covering all of it, so the exported PDF matches
everything the dashboard actually shows.

Long tables use a repeating header row and paginate automatically;
ReportLab splits a Table across pages at row boundaries by default.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)

# Python 3.11 (the version this app ships with, per Dockerfile) forbids a
# backslash escape (e.g. "\u2014") inside the {...} expression part of an
# f-string -- that restriction was only lifted in Python 3.12. Any such
# literal has to live outside the f-string's braces, so pull the couple of
# symbols used that way out into plain module-level constants instead.
_EM_DASH = "\u2014"
_MIDDLE_DOT = "\u00b7"


# ============================================================
# Extraction helpers
#
# These mirror dashboard_api/api.py's own extraction helpers so
# this module can be handed the raw pipeline result dict and stay
# self-contained. If api.py's helpers ever change shape, keep
# these in sync.
# ============================================================

def _cases(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("cases", [])
    return value if isinstance(value, list) else []


def _metrics(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("metrics", {})
    return value if isinstance(value, dict) else {}


def _psr_alerts(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("psr_alerts", [])
    return value if isinstance(value, list) else []


def _a2a_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract A2A settlement results.

    Primary key: a2a_settlements
    Backward-compatible fallback: a2a_results
    """

    value = result.get("a2a_settlements", [])
    if isinstance(value, list):
        return value

    value = result.get("a2a_results", [])
    return value if isinstance(value, list) else []


def _ledger(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("ledger", [])
    return value if isinstance(value, list) else []


def _a2a_counts(result: dict[str, Any]) -> tuple[int, int]:
    """
    Return the same A2A counts represented by the React dashboard.

    See dashboard_api/api.py._a2a_counts for the full rationale.
    Duplicated here (rather than imported) so this module has no
    import-time dependency on api.py, avoiding any circular import
    between the two.
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
                ["outcome", "a2a_outcome", "settlement_status", "status"],
                "",
            )
        ).strip().upper()

        raw_eligible = item.get("eligible")

        if isinstance(raw_eligible, bool):
            is_eligible = raw_eligible
        elif isinstance(raw_eligible, str):
            is_eligible = raw_eligible.strip().lower() in {
                "true", "1", "yes", "eligible",
            }
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

        if outcome in {"SETTLED", "REJECTED"}:
            is_eligible = True

        if is_eligible:
            eligible += 1

        if outcome == "SETTLED":
            settled += 1

    return eligible, settled


# ============================================================
# Formatting helpers
# ============================================================

def _money(value: Any) -> str:
    """Format monetary values as INR (avoids glyph issues in Helvetica)."""

    try:
        return f"INR {float(value):,.2f}"
    except (TypeError, ValueError):
        return "INR 0.00"


def _percent(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _safe_text(value: Any) -> str:
    if value is None or value == "":
        return "\u2014"

    text = str(value)

    # Ledger/ROI "reason" strings are built elsewhere in the pipeline
    # (see roi_engine/roi.py) with the literal "₹" glyph baked in.
    # ReportLab's built-in Helvetica font has no glyph for "₹" and
    # silently renders it as a black box, so normalize it the same
    # way _money() does everywhere else in this report.
    text = text.replace("\u20b9", "INR ")

    return text


def _first_value(item: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return default


# ============================================================
# Board Report PDF
# ============================================================

def build_board_report_pdf(
    result: dict[str, Any],
    live_data: dict[str, Any] | None = None,
    filter_note: str | None = None,
) -> BytesIO:
    """
    Generate the executive Revive Board Report.

    Every financial and operational number, every PSR alert, every
    case, every settlement, and every ledger event comes directly
    from the authoritative pipeline result. This function does NOT
    recalculate recovery logic and does NOT summarize away rows —
    "Export snapshot" is meant to be a complete record.

    `result` is the synthetic 105-case benchmark pipeline result --
    the same one every other section of this report is built from.

    `live_data`, if provided, carries everything on the dashboard
    that is intentionally kept OUT of that synthetic pipeline
    result (see dashboard_api/api.py's cases vs live-cases,
    metrics vs live_metrics, psr_alerts vs live psr alerts, and
    a2a vs live A2A settlements). Without it, the PDF silently
    omits every one of those dashboard sections even though they
    are real, currently-visible data -- this parameter is what
    makes "Export snapshot" actually match the dashboard. Expected
    keys (all optional, all default to empty):
        - "live_cases": list[dict]        (live_case_store.load())
        - "live_metrics": dict             (build_live_metrics(...))
        - "live_psr_alerts": list[dict]    (detect_alerts(...) over live cases)
        - "live_a2a_settlements": list[dict] (live_a2a_settlement_store.list_all())
        - "real_capture_case": dict | None (real_captured_case.json, if any)

    `filter_note`, if provided, is rendered as a banner right under
    the report header and again as context above the Case Register,
    so a filtered export is clearly labeled as such rather than
    looking like a full, unfiltered board report.
    """

    metrics = _metrics(result)
    alerts = _psr_alerts(result)
    settlements = _a2a_results(result)
    ledger = _ledger(result)
    cases = _cases(result)

    live_data = live_data if isinstance(live_data, dict) else {}

    live_cases = live_data.get("live_cases", [])
    live_cases = live_cases if isinstance(live_cases, list) else []

    live_metrics = live_data.get("live_metrics", {})
    live_metrics = live_metrics if isinstance(live_metrics, dict) else {}

    live_psr_alerts = live_data.get("live_psr_alerts", [])
    live_psr_alerts = live_psr_alerts if isinstance(live_psr_alerts, list) else []

    live_a2a_settlements = live_data.get("live_a2a_settlements", [])
    live_a2a_settlements = (
        live_a2a_settlements if isinstance(live_a2a_settlements, list) else []
    )

    real_capture_case = live_data.get("real_capture_case")
    real_capture_case = (
        real_capture_case if isinstance(real_capture_case, dict) else None
    )

    # Compute A2A eligible/settled ONCE from the settlement rows
    # themselves, so every table in this report (and the dashboard)
    # shows the same number. Do NOT use metrics["a2a_eligible_cases"]
    # here — that metric counts every case the A2A engine evaluated,
    # including ones it internally BLOCKED, which is not the same as
    # the settlement table's ELIGIBLE count.
    a2a_eligible_count, a2a_settled_count = _a2a_counts(result)

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Revive Board Report",
        author="Revive AI Revenue Recovery",
    )

    styles = getSampleStyleSheet()

    # --------------------------------------------------------
    # Styles
    # --------------------------------------------------------

    title_style = ParagraphStyle(
        "BoardTitle", parent=styles["Title"],
        fontName="Helvetica-Bold", fontSize=23, leading=27,
        alignment=TA_LEFT, textColor=colors.HexColor("#111827"),
        spaceAfter=4,
    )

    subtitle_style = ParagraphStyle(
        "BoardSubtitle", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9, leading=13,
        textColor=colors.HexColor("#667085"), spaceAfter=14,
    )

    section_style = ParagraphStyle(
        "BoardSection", parent=styles["Heading2"],
        fontName="Helvetica-Bold", fontSize=11, leading=14,
        textColor=colors.HexColor("#111827"),
        spaceBefore=14, spaceAfter=7,
    )

    normal_style = ParagraphStyle(
        "BoardNormal", parent=styles["Normal"],
        fontName="Helvetica", fontSize=8.5, leading=13,
        textColor=colors.HexColor("#344054"),
    )

    small_style = ParagraphStyle(
        "BoardSmall", parent=styles["Normal"],
        fontName="Helvetica", fontSize=7, leading=10,
        textColor=colors.HexColor("#667085"),
    )

    alert_style = ParagraphStyle(
        "BoardAlert", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=8.5, leading=13,
        textColor=colors.HexColor("#B42318"),
    )

    dividend_style = ParagraphStyle(
        "Dividend", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=10, leading=15,
        textColor=colors.HexColor("#027A48"),
    )

    metric_style = ParagraphStyle(
        "Metric", parent=normal_style,
        fontName="Helvetica-Bold", fontSize=14, leading=17,
        textColor=colors.HexColor("#101828"),
    )

    green_metric_style = ParagraphStyle(
        "GreenMetric", parent=metric_style,
        textColor=colors.HexColor("#027A48"),
    )

    # Compact cell style for long detail tables (cases / settlements
    # / ledger). Paragraph (not raw strings) so long text wraps
    # inside its column instead of overflowing the page.
    cell_style = ParagraphStyle(
        "Cell", parent=styles["Normal"],
        fontName="Helvetica", fontSize=6.8, leading=9,
        textColor=colors.HexColor("#344054"),
    )

    cell_header_style = ParagraphStyle(
        "CellHeader", parent=cell_style,
        fontName="Helvetica-Bold", fontSize=6.8,
        textColor=colors.HexColor("#344054"),
    )

    cell_bold_style = ParagraphStyle(
        "CellBold", parent=cell_style,
        fontName="Helvetica-Bold",
    )

    story: list[Any] = []

    def cell(value: Any, bold: bool = False) -> Paragraph:
        # Detail-table cells (case/settlement/ledger rows) carry
        # free-form text generated elsewhere in the pipeline (e.g.
        # ROI "reason" strings). Escape it so any stray "&"/"<"/">"
        # can't be misread as ReportLab's Paragraph markup.
        text = _xml_escape(_safe_text(value))
        return Paragraph(text, cell_bold_style if bold else cell_style)

    def header_cell(value: str) -> Paragraph:
        return Paragraph(value, cell_header_style)

    def detail_table(
        header: list[str],
        rows: list[list[Any]],
        col_widths: list[float],
        header_bg: str = "#F2F4F7",
    ) -> Table:
        data = [[header_cell(h) for h in header]] + rows

        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E4E7EC")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return table

    # ========================================================
    # HEADER
    # ========================================================

    story.append(Paragraph("REVIVE", title_style))

    story.append(
        Paragraph(
            "AI REVENUE RECOVERY",
            ParagraphStyle(
                "HeaderLabel", parent=subtitle_style,
                fontName="Helvetica-Bold", fontSize=8,
                textColor=colors.HexColor("#475467"),
            ),
        )
    )

    # Docker containers commonly run in UTC. The dashboard is
    # intended for the India deployment, so render the report
    # timestamp explicitly in IST instead of depending on the
    # container's timezone.
    report_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %B %Y \u2022 %H:%M")

    story.append(
        Paragraph(f"BOARD REPORT \u2022 Generated {report_time}", subtitle_style)
    )

    if filter_note:
        filter_banner = Table(
            [[Paragraph(_xml_escape(filter_note), normal_style)]],
            colWidths=[176 * mm],
        )
        filter_banner.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFF8FF")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#B2DDFF")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(filter_banner)
        story.append(Spacer(1, 10))

    # ========================================================
    # EXECUTIVE OUTCOME
    # ========================================================

    story.append(Paragraph("EXECUTIVE OUTCOME", section_style))

    executive_data = [
        [
            Paragraph("<b>ADDRESSABLE REVENUE</b>", small_style),
            Paragraph("<b>RECOVERED REVENUE</b>", small_style),
            Paragraph("<b>RECOVERY RATE</b>", small_style),
            Paragraph("<b>NET RECOVERED VALUE</b>", small_style),
        ],
        [
            Paragraph(_money(metrics.get("addressable_revenue", 0)), metric_style),
            Paragraph(_money(metrics.get("recovered_revenue", 0)), green_metric_style),
            Paragraph(
                _percent(float(metrics.get("recovery_rate", 0) or 0) * 100),
                metric_style,
            ),
            Paragraph(_money(metrics.get("net_recovered_value", 0)), green_metric_style),
        ],
    ]

    executive_table = Table(executive_data, colWidths=[43 * mm] * 4)
    executive_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D0D5DD")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E4E7EC")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )

    story.append(executive_table)

    # ========================================================
    # RECOVERY PERFORMANCE
    # ========================================================

    story.append(Paragraph("RECOVERY PERFORMANCE", section_style))

    performance_data = [
        ["TOTAL CASES", "PURSUED", "STOPPED", "RECOVERED"],
        [
            _safe_text(metrics.get("total_cases", len(cases))),
            _safe_text(metrics.get("pursued_cases", 0)),
            _safe_text(metrics.get("stopped_cases", 0)),
            _safe_text(metrics.get("recovered_cases", 0)),
        ],
        ["RECOVERY COST", "UNRECOVERED REVENUE", "A2A ELIGIBLE", "A2A SETTLED"],
        [
            _money(metrics.get("recovery_cost", 0)),
            _money(metrics.get("unrecovered_revenue", 0)),
            _safe_text(a2a_eligible_count),
            _safe_text(a2a_settled_count),
        ],
    ]

    performance_table = Table(performance_data, colWidths=[43 * mm] * 4)
    performance_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#F2F4F7")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#344054")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )

    story.append(performance_table)

    # ========================================================
    # ROI DIVIDEND
    # ========================================================

    recovered = _money(metrics.get("recovered_revenue", 0))
    addressable = _money(metrics.get("addressable_revenue", 0))
    recovery_cost = _money(metrics.get("recovery_cost", 0))

    try:
        recovery_rate_value = float(metrics.get("recovery_rate", 0) or 0)
    except (TypeError, ValueError):
        recovery_rate_value = 0.0

    recovery_rate = _percent(recovery_rate_value * 100)

    story.append(Spacer(1, 7))

    dividend_text = (
        f"Revive recovered {recovered} from {addressable} of addressable "
        f"revenue at a recovery cost of only {recovery_cost}, delivering "
        f"a {recovery_rate} recovery rate."
    )

    dividend_table = Table(
        [
            [Paragraph("ROI DIVIDEND", small_style)],
            [Paragraph(dividend_text, dividend_style)],
        ],
        colWidths=[176 * mm],
    )
    dividend_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ECFDF3")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#ABEFC6")),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )

    story.append(dividend_table)

    def render_psr_alert_group(alert_list: list[dict[str, Any]]) -> None:
        """
        Render one alert-box per PSR alert. Shared by the synthetic
        105-case benchmark's alerts and the live Razorpay Test Mode
        alerts (same RouteAlert schema from psr_guardian/guardian.py
        in both cases), so both sections stay visually identical.
        """

        for index, alert in enumerate(alert_list, start=1):
            alert_title = _first_value(
                alert, ["title", "alert_type", "type"],
                "Systemic recovery-risk alert",
            )

            severity = _safe_text(alert.get("severity", "")).upper()

            alert_description = _first_value(
                alert, ["description", "message", "reason"], None,
            )

            if not alert_description:
                # PSR Guardian's actual alert schema (RouteAlert in
                # psr_guardian/guardian.py) doesn't carry a
                # "description"/"message"/"reason" field — it carries
                # structured fields instead (bank, card_network,
                # decline_code, concentrated_cases, group_size,
                # concentration_ratio). Build the observation directly
                # from those, so the report shows the real, specific
                # finding instead of a generic placeholder sentence.
                bank = _safe_text(alert.get("bank", "an issuing bank"))
                network = _safe_text(alert.get("card_network", ""))
                decline_code = _safe_text(alert.get("decline_code", "failures"))
                concentrated = alert.get("concentrated_cases")
                group_size = alert.get("group_size")
                ratio = alert.get("concentration_ratio")

                if concentrated is not None and group_size is not None and ratio is not None:
                    route = f"{bank}/{network}" if network else bank
                    alert_description = (
                        f"{concentrated} of {group_size} {decline_code} failures "
                        f"({float(ratio) * 100:.0f}% concentration) on the {route} "
                        f"route occurred within a single time window \u2014 a "
                        f"systemic pattern, not independent customer failures."
                    )
                else:
                    alert_description = "PSR Guardian detected a policy or recovery risk."

            window_start = alert.get("window_start")
            window_end = alert.get("window_end")
            window_text = (
                f"{_safe_text(window_start)} \u2192 {_safe_text(window_end)}"
                if window_start or window_end
                else None
            )

            alert_recommendation = _first_value(
                alert, ["recommendation", "recommended_action"],
                "Review the affected recovery pattern and policy constraints.",
            )

            heading = f"<b>ALERT {index} of {len(alert_list)}</b>"
            if severity and severity != "\u2014":
                heading += f" \u2014 {severity} SEVERITY"
            heading += f"<br/>{_safe_text(alert_title)}"

            psr_rows = [
                [Paragraph(heading, alert_style)],
                [Paragraph(f"<b>Observation:</b> {_safe_text(alert_description)}", normal_style)],
            ]

            if window_text:
                psr_rows.append(
                    [Paragraph(f"<b>Window:</b> {window_text}", normal_style)]
                )

            psr_rows.append(
                [Paragraph(f"<b>Recommendation:</b> {_safe_text(alert_recommendation)}", normal_style)]
            )

            psr_table = Table(psr_rows, colWidths=[176 * mm])
            psr_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7ED")),
                        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#FED7AA")),
                        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#FED7AA")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 11),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                        ("TOPPADDING", (0, 0), (-1, -1), 8),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )

            story.append(psr_table)
            story.append(Spacer(1, 6))

    def render_psr_empty_state(message: str) -> None:
        psr_table = Table(
            [[Paragraph(f"<b>PSR STATUS</b><br/>{message}", normal_style)]],
            colWidths=[176 * mm],
        )
        psr_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7ED")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#FED7AA")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 11),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(psr_table)

    story.append(Paragraph("PSR GUARDIAN", section_style))

    if alerts:
        story.append(
            Paragraph(
                f"{len(alerts)} systemic recovery-risk alert"
                f"{'s' if len(alerts) != 1 else ''} detected in the "
                f"synthetic 105-case benchmark pipeline result.",
                normal_style,
            )
        )
        story.append(Spacer(1, 5))
        render_psr_alert_group(alerts)
    else:
        render_psr_empty_state("No active systemic alerts.")

    # ========================================================
    # A2A SETTLEMENT
    # ========================================================

    story.append(Paragraph("AGENT-TO-AGENT SETTLEMENT", section_style))

    eligible_count, settled_count = a2a_eligible_count, a2a_settled_count

    try:
        eligible_numeric = float(eligible_count)
        settled_numeric = float(settled_count)
    except (TypeError, ValueError):
        eligible_numeric = 0.0
        settled_numeric = 0.0

    settlement_rate = (
        settled_numeric / eligible_numeric * 100 if eligible_numeric > 0 else 0.0
    )

    a2a_summary_table = Table(
        [
            ["ELIGIBLE", "SETTLED", "SETTLEMENT RATE"],
            [str(int(eligible_count)), str(int(settled_count)), _percent(settlement_rate)],
        ],
        colWidths=[58 * mm, 58 * mm, 60 * mm],
    )
    a2a_summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    story.append(a2a_summary_table)
    story.append(Spacer(1, 8))

    # Full itemized settlement register — every A2A negotiation
    # result, not just the aggregate counts above.
    if settlements:
        story.append(
            Paragraph(
                f"Settlement register \u2014 {len(settlements)} negotiation "
                f"result{'s' if len(settlements) != 1 else ''}:",
                normal_style,
            )
        )
        story.append(Spacer(1, 4))

        settlement_rows = []
        for item in settlements:
            if not isinstance(item, dict):
                continue

            case_id = _first_value(item, ["case_id"], "\u2014")
            invoice_id = _first_value(item, ["invoice_id"], "\u2014")
            final_amount = item.get("final_amount")
            discount = item.get("discount_percent")
            rounds = item.get("rounds")
            outcome = _first_value(item, ["outcome", "a2a_outcome"], "\u2014")
            settlement_status = _first_value(item, ["settlement_status"], "\u2014")
            payment_status = _first_value(item, ["payment_status"], "\u2014")

            settlement_rows.append(
                [
                    cell(case_id),
                    cell(invoice_id),
                    cell(_money(final_amount) if final_amount is not None else "\u2014"),
                    cell(
                        f"{float(discount):.1f}%"
                        if discount is not None
                        else "\u2014"
                    ),
                    cell(rounds),
                    cell(outcome, bold=True),
                    cell(settlement_status),
                    cell(payment_status),
                ]
            )

        story.append(
            detail_table(
                header=[
                    "Case ID", "Invoice ID", "Final Amount", "Discount",
                    "Rounds", "Outcome", "Settlement Status", "Payment Status",
                ],
                rows=settlement_rows,
                col_widths=[
                    22 * mm, 22 * mm, 24 * mm, 16 * mm,
                    14 * mm, 22 * mm, 28 * mm, 28 * mm,
                ],
            )
        )
    else:
        story.append(Paragraph("No A2A settlement attempts in this pipeline result.", normal_style))

    # ========================================================
    # CASE REGISTER — every case, not just aggregate counts
    # ========================================================

    story.append(PageBreak())
    story.append(Paragraph("CASE REGISTER", section_style))
    story.append(
        Paragraph(
            (
                f"{_xml_escape(filter_note)} Showing the matching cases below."
                if filter_note
                else (
                    f"Complete record of all {len(cases)} case"
                    f"{'s' if len(cases) != 1 else ''} evaluated by "
                    f"the pipeline."
                )
            ),
            normal_style,
        )
    )
    story.append(Spacer(1, 4))

    if cases:
        case_rows = []
        for case in cases:
            if not isinstance(case, dict):
                continue

            case_id = case.get("case_id", "\u2014")
            customer = _first_value(
                case, ["customer_name", "customer_id"], "\u2014",
            )
            amount = case.get("amount")
            root_cause = case.get("root_cause", "\u2014")
            decision = case.get("roi_decision", "\u2014")
            outcome = case.get("outcome", "\u2014")
            recovered_amount = case.get("recovered_amount")

            case_rows.append(
                [
                    cell(case_id),
                    cell(customer),
                    cell(_money(amount) if amount is not None else "\u2014"),
                    cell(root_cause),
                    cell(decision),
                    cell(outcome, bold=True),
                    cell(
                        _money(recovered_amount)
                        if recovered_amount not in (None, 0)
                        else "\u2014"
                    ),
                ]
            )

        story.append(
            detail_table(
                header=[
                    "Case ID", "Customer", "Amount", "Root Cause",
                    "Decision", "Outcome", "Recovered",
                ],
                rows=case_rows,
                col_widths=[
                    20 * mm, 26 * mm, 20 * mm, 36 * mm,
                    16 * mm, 28 * mm, 30 * mm,
                ],
            )
        )
    else:
        story.append(Paragraph("No cases in this pipeline result.", normal_style))

    # ========================================================
    # RECOVERY LEDGER — every event, not just a count
    # ========================================================

    story.append(PageBreak())
    story.append(Paragraph("RECOVERY LEDGER", section_style))
    story.append(
        Paragraph(
            f"{len(ledger)} authoritative recovery ledger event"
            f"{'s' if len(ledger) != 1 else ''} are represented in the "
            f"current pipeline result.",
            normal_style,
        )
    )
    story.append(Spacer(1, 4))

    if ledger:
        ledger_rows = []
        for event in ledger:
            if not isinstance(event, dict):
                continue

            ledger_rows.append(
                [
                    cell(
                        f"{event.get('case_id', _EM_DASH)} "
                        f"{_MIDDLE_DOT} #{event.get('attempt_number', _EM_DASH)}"
                    ),
                    cell(event.get("timestamp", "\u2014")),
                    cell(event.get("action", "\u2014")),
                    cell(event.get("channel", "\u2014")),
                    cell(event.get("decision", "\u2014")),
                    cell(event.get("outcome", "\u2014"), bold=True),
                    cell(
                        _money(event.get("expected_value"))
                        if event.get("expected_value") is not None
                        else "\u2014"
                    ),
                    cell(
                        _money(event.get("action_cost"))
                        if event.get("action_cost") is not None
                        else "\u2014"
                    ),
                    cell(event.get("reason", "\u2014")),
                ]
            )

        story.append(
            detail_table(
                header=[
                    "Case / Attempt", "Timestamp", "Action", "Channel",
                    "Decision", "Outcome", "Expected Value", "Cost", "Reason",
                ],
                rows=ledger_rows,
                col_widths=[
                    22 * mm, 20 * mm, 16 * mm, 16 * mm,
                    13 * mm, 27 * mm, 18 * mm, 13 * mm, 31 * mm,
                ],
            )
        )
    else:
        story.append(Paragraph("No ledger events in this pipeline result.", normal_style))

    # ========================================================
    # LIVE RAZORPAY TEST MODE
    #
    # Everything above this point comes from the synthetic
    # 105-case benchmark pipeline (`result`). The dashboard also
    # shows real Razorpay Test Mode activity captured via webhook,
    # which is intentionally kept isolated from that synthetic
    # pipeline (see api.py: cases vs live-cases, metrics vs
    # live_metrics, psr_alerts vs live psr alerts, a2a vs live A2A
    # settlements). Without this section the PDF only ever shows
    # the synthetic benchmark, even though the dashboard shows
    # this real data alongside it.
    # ========================================================

    story.append(PageBreak())
    story.append(Paragraph("LIVE RAZORPAY TEST MODE", section_style))
    story.append(
        Paragraph(
            "The sections below reflect real Razorpay Test Mode activity "
            "captured via webhook and tracked completely separately from "
            "the synthetic 105-case benchmark above.",
            normal_style,
        )
    )
    story.append(Spacer(1, 6))

    # --------------------------------------------------------
    # Live metrics summary
    # --------------------------------------------------------

    funnel = live_metrics.get("funnel", {})
    funnel = funnel if isinstance(funnel, dict) else {}

    try:
        live_recovery_rate_pct = float(live_metrics.get("live_recovery_rate", 0) or 0) * 100
    except (TypeError, ValueError):
        live_recovery_rate_pct = 0.0

    live_metrics_data = [
        ["LIVE CASES", "PENDING RECOVERY", "RECOVERED", "LIVE RECOVERY RATE"],
        [
            _safe_text(live_metrics.get("live_cases", len(live_cases))),
            _safe_text(live_metrics.get("pending_recovery", 0)),
            _safe_text(live_metrics.get("recovered_cases", 0)),
            _percent(live_recovery_rate_pct),
        ],
        ["TOTAL AMOUNT", "PENDING AMOUNT", "RECOVERED AMOUNT", "RETRY LINKS ISSUED"],
        [
            _money(live_metrics.get("total_amount", 0)),
            _money(live_metrics.get("pending_amount", 0)),
            _money(live_metrics.get("recovered_amount", 0)),
            _safe_text(live_metrics.get("retry_links", 0)),
        ],
    ]

    live_metrics_table = Table(live_metrics_data, colWidths=[43 * mm] * 4)
    live_metrics_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#F2F4F7")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#344054")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )

    story.append(live_metrics_table)
    story.append(Spacer(1, 10))

    # --------------------------------------------------------
    # Live PSR Guardian alerts
    # --------------------------------------------------------

    story.append(Paragraph("LIVE PSR GUARDIAN ALERTS", section_style))

    if live_psr_alerts:
        story.append(
            Paragraph(
                f"{len(live_psr_alerts)} systemic recovery-risk alert"
                f"{'s' if len(live_psr_alerts) != 1 else ''} detected in "
                f"real Razorpay Test Mode webhook-captured failures.",
                normal_style,
            )
        )
        story.append(Spacer(1, 5))
        render_psr_alert_group(live_psr_alerts)
    else:
        render_psr_empty_state("No active systemic alerts in live Razorpay Test Mode data.")

    story.append(Spacer(1, 4))

    # --------------------------------------------------------
    # Live A2A settlements
    # --------------------------------------------------------

    story.append(Paragraph("LIVE A2A SETTLEMENTS", section_style))

    if live_a2a_settlements:
        story.append(
            Paragraph(
                f"{len(live_a2a_settlements)} live settlement agreement"
                f"{'s' if len(live_a2a_settlements) != 1 else ''}:",
                normal_style,
            )
        )
        story.append(Spacer(1, 4))

        live_a2a_rows = []
        for item in live_a2a_settlements:
            if not isinstance(item, dict):
                continue

            live_a2a_rows.append(
                [
                    cell(_first_value(item, ["agreement_id"], "\u2014")),
                    cell(_first_value(item, ["case_id"], "\u2014")),
                    cell(_first_value(item, ["invoice_id"], "\u2014")),
                    cell(
                        _money(item.get("agreed_amount"))
                        if item.get("agreed_amount") is not None
                        else "\u2014"
                    ),
                    cell(_first_value(item, ["settlement_status"], "\u2014"), bold=True),
                    cell(_first_value(item, ["payment_status"], "\u2014")),
                ]
            )

        story.append(
            detail_table(
                header=[
                    "Agreement ID", "Case ID", "Invoice ID",
                    "Agreed Amount", "Settlement Status", "Payment Status",
                ],
                rows=live_a2a_rows,
                col_widths=[
                    32 * mm, 24 * mm, 24 * mm, 26 * mm, 32 * mm, 32 * mm,
                ],
            )
        )
    else:
        story.append(
            Paragraph("No live A2A settlement agreements yet.", normal_style)
        )

    story.append(Spacer(1, 4))

    # --------------------------------------------------------
    # Live case register — every webhook-captured case
    # --------------------------------------------------------

    story.append(PageBreak())
    story.append(Paragraph("LIVE CASE REGISTER", section_style))
    story.append(
        Paragraph(
            f"Complete record of all {len(live_cases)} real Razorpay "
            f"Test Mode case{'s' if len(live_cases) != 1 else ''} "
            f"captured via webhook.",
            normal_style,
        )
    )
    story.append(Spacer(1, 4))

    if live_cases:
        live_case_rows = []
        for case in live_cases:
            if not isinstance(case, dict):
                continue

            case_id = case.get("case_id", "\u2014")
            customer = _first_value(
                case, ["customer_name", "customer_id"], "\u2014",
            )
            amount = case.get("amount")
            root_cause = _first_value(
                case, ["decline_code", "root_cause"], "\u2014",
            )
            status = case.get("recovery_status", "\u2014")
            recovered_amount = case.get("recovered_amount")
            retry_count = case.get("retry_count", 0)

            live_case_rows.append(
                [
                    cell(case_id),
                    cell(customer),
                    cell(_money(amount) if amount is not None else "\u2014"),
                    cell(root_cause),
                    cell(status, bold=True),
                    cell(
                        _money(recovered_amount)
                        if recovered_amount not in (None, 0)
                        else "\u2014"
                    ),
                    cell(retry_count),
                ]
            )

        story.append(
            detail_table(
                header=[
                    "Case ID", "Customer", "Amount", "Decline Code",
                    "Recovery Status", "Recovered", "Retries",
                ],
                rows=live_case_rows,
                col_widths=[
                    26 * mm, 26 * mm, 20 * mm, 26 * mm,
                    28 * mm, 26 * mm, 14 * mm,
                ],
            )
        )
    else:
        story.append(
            Paragraph("No live Razorpay Test Mode cases captured yet.", normal_style)
        )

    # --------------------------------------------------------
    # Real Razorpay sandbox capture (single verified case)
    # --------------------------------------------------------

    story.append(Spacer(1, 10))
    story.append(Paragraph("REAL RAZORPAY SANDBOX CAPTURE", section_style))

    if real_capture_case:
        capture_customer = _first_value(
            real_capture_case, ["customer_name", "customer_id"], _EM_DASH,
        )
        capture_amount = (
            _money(real_capture_case.get("amount"))
            if real_capture_case.get("amount") is not None
            else _EM_DASH
        )
        capture_status = real_capture_case.get("recovery_status", _EM_DASH)

        capture_rows = [
            [Paragraph(
                "<b>This is a single, manually verified Razorpay sandbox "
                "capture.</b> It is tracked separately from both the "
                "synthetic benchmark and the webhook-captured live cases "
                "above.",
                normal_style,
            )],
            [Paragraph(
                f"<b>Case ID:</b> {_xml_escape(_safe_text(real_capture_case.get('case_id')))} "
                f"&nbsp;&nbsp; <b>Customer:</b> "
                f"{_xml_escape(_safe_text(capture_customer))}",
                normal_style,
            )],
            [Paragraph(
                f"<b>Amount:</b> "
                f"{_xml_escape(_safe_text(capture_amount))} "
                f"&nbsp;&nbsp; <b>Status:</b> "
                f"{_xml_escape(_safe_text(capture_status))}",
                normal_style,
            )],
        ]

        capture_table = Table(capture_rows, colWidths=[176 * mm])
        capture_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D0D5DD")),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E4E7EC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 11),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(capture_table)
    else:
        story.append(
            Paragraph("No real Razorpay sandbox capture on file.", normal_style)
        )

    # ========================================================
    # FOOTER
    # ========================================================

    story.append(Spacer(1, 18))

    footer_table = Table(
        [
            [
                Paragraph(
                    "Generated from the authoritative Revive recovery "
                    "pipeline. This report contains the complete "
                    "pipeline-derived case, settlement, and ledger "
                    "records and does not recalculate recovery "
                    "decisions.",
                    small_style,
                )
            ]
        ],
        colWidths=[176 * mm],
    )
    footer_table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    story.append(footer_table)

    # ========================================================
    # BUILD PDF
    # ========================================================

    document.build(story)
    buffer.seek(0)

    return buffer
