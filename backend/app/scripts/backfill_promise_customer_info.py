"""
One-off backfill: fill in missing customer_name / customer_id /
customer_email on existing Promise-to-Pay records.

Why this exists
----------------
Before the identifier-mapping fix (see README-mapping-audit.md),
`POST /api/promises` could persist a record with `customer_name`
and/or `customer_email` left as `null` even though the authoritative
case actually had that information (e.g. RV-00100 has
customer_name "Kavya" in cases.json but the stored promise record
has customer_name: null). The live-code fix stops this happening
going forward — this script repairs records that were already
written before the fix.

This script is intentionally conservative:
  - It only ever FILLS a null. It never overwrites a value that is
    already present, even if it looks wrong — that's a judgement
    call for a human, not this script.
  - It looks up each case by case_id in both cases.json (synthetic/
    demo cases) and live_cases.json (real Razorpay-webhook cases),
    live_cases.json taking priority since it's the more likely
    source for a case with a real customer_email.
  - It writes nothing if there is nothing to fix (`--dry-run` prints
    what it *would* change without touching the file).

Usage
-----
    python -m app.scripts.backfill_promise_customer_info --dry-run
    python -m app.scripts.backfill_promise_customer_info

Run from backend/, with the same working directory / environment
your app normally runs in (it reads the same
app/data/promise_tracker.json, app/data/cases.json and
app/data/live_cases.json paths the app itself uses, unless
overridden via CUSTOMER_ALERTS_FILE-style env vars are not needed
here — this script reads the data files directly).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CASES_FILE = DATA_DIR / "cases.json"
LIVE_CASES_FILE = DATA_DIR / "live_cases.json"
PROMISE_TRACKER_FILE = DATA_DIR / "promise_tracker.json"


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_case_lookup() -> dict[str, dict[str, Any]]:
    """
    case_id -> {"customer_id":..., "customer_name":..., "customer_email":...}

    live_cases.json entries are applied last, so they win over
    cases.json on conflict (a live case is more likely to carry a
    real customer_email once the webhook-mapping fix has been in
    place for a while).
    """
    lookup: dict[str, dict[str, Any]] = {}

    for path in (CASES_FILE, LIVE_CASES_FILE):
        data = _load_json(path)
        if not isinstance(data, list):
            continue
        for case in data:
            if not isinstance(case, dict):
                continue
            case_id = case.get("case_id")
            if not case_id:
                continue
            entry = lookup.setdefault(case_id, {})
            for field in ("customer_id", "customer_name", "customer_email"):
                value = case.get(field)
                if value is not None:
                    entry[field] = value

    return lookup


def backfill(dry_run: bool = True) -> dict[str, Any]:
    tracker_data = _load_json(PROMISE_TRACKER_FILE)
    if not tracker_data or "records" not in tracker_data:
        print(f"Nothing to do — {PROMISE_TRACKER_FILE} not found or has no 'records'.")
        return {"changed": 0, "checked": 0}

    case_lookup = _build_case_lookup()

    changed = 0
    checked = 0
    changes_log: list[str] = []

    for case_id, record in tracker_data["records"].items():
        checked += 1
        case_info = case_lookup.get(case_id)
        if not case_info:
            continue

        for field in ("customer_id", "customer_name", "customer_email"):
            if record.get(field) is None and case_info.get(field) is not None:
                old = record.get(field)
                new = case_info[field]
                record[field] = new
                changed += 1
                changes_log.append(
                    f"{case_id}.{field}: {old!r} -> {new!r}"
                )

    if changes_log:
        print(f"{'[DRY RUN] ' if dry_run else ''}Backfilling {len(changes_log)} field(s):")
        for line in changes_log:
            print(f"  {line}")
    else:
        print("No missing fields found that could be backfilled from a known case.")

    if not dry_run and changes_log:
        with PROMISE_TRACKER_FILE.open("w", encoding="utf-8") as f:
            json.dump(tracker_data, f, indent=2, ensure_ascii=False)
        print(f"Wrote updates to {PROMISE_TRACKER_FILE}")

    return {"changed": changed, "checked": checked}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing the file.",
    )
    args = parser.parse_args()

    result = backfill(dry_run=args.dry_run)
    print(
        f"Checked {result['checked']} promise record(s), "
        f"{result['changed']} field(s) "
        f"{'would be' if args.dry_run else 'were'} backfilled."
    )


if __name__ == "__main__":
    main()
