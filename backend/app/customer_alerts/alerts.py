"""Real email customer notifications for Revive Promise-to-Pay.

Provider:
- Email: Resend REST API

Delivery is real: an event is marked ``sent`` only after Resend accepts
the request. Missing configuration/recipient data is recorded as skipped;
provider errors are recorded as failed.

Supported events:
- PROMISE_CREATED
- DUE_SOON
- PAYMENT_VERIFIED
- PROMISE_BROKEN
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ALERT_FILE = Path(
    os.getenv(
        "CUSTOMER_ALERTS_FILE",
        str(DATA_DIR / "customer_alerts.json"),
    )
)
TIMEOUT = float(os.getenv("CUSTOMER_ALERT_HTTP_TIMEOUT_SECONDS", "15"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CustomerAlertService:
    """Durable, idempotent Resend email delivery service."""

    SUPPORTED_EVENTS = {
        "PROMISE_CREATED",
        "DUE_SOON",
        "PAYMENT_VERIFIED",
        "PROMISE_BROKEN",
    }

    def __init__(self, path: Path = ALERT_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                if self.path.exists():
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._events = data if isinstance(data, list) else []
                else:
                    self._events = []
            except (OSError, json.JSONDecodeError, TypeError):
                self._events = []

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._events, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _find_existing(self, key: str) -> dict[str, Any] | None:
        for event in reversed(self._events):
            if event.get("idempotency_key") == key:
                return event
        return None

    def _record(self, **event: Any) -> dict[str, Any]:
        item = {"created_at": _now(), **event}
        self._events.append(item)
        self._save()
        return item

    def _send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        api_key = os.getenv("RESEND_API_KEY", "").strip()
        sender = os.getenv("RESEND_FROM_EMAIL", "").strip()

        if not api_key:
            raise RuntimeError("Resend is not configured: set RESEND_API_KEY.")
        if not sender:
            raise RuntimeError("Resend is not configured: set RESEND_FROM_EMAIL.")

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=TIMEOUT,
        )

        if response.status_code >= 300:
            raise RuntimeError(
                f"Resend email failed ({response.status_code}): "
                f"{response.text[:500]}"
            )

        data = response.json()
        return {
            "provider": "resend",
            "provider_id": data.get("id"),
            "provider_status": "accepted",
        }

    def send(
        self,
        promise: Any,
        event_type: str,
        *,
        payment_link_url: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Send one real email alert for a promise event."""

        event_type = str(event_type or "").upper()

        if event_type not in self.SUPPORTED_EVENTS:
            raise ValueError(
                f"Unsupported customer alert event: {event_type}"
            )

        promise_id = str(getattr(promise, "promise_id", "") or "")
        case_id = str(getattr(promise, "case_id", "") or "")
        recipient = str(getattr(promise, "customer_email", "") or "").strip()
        key = f"{promise_id}:{event_type}:email"

        with self._lock:
            existing = self._find_existing(key)
            if existing is not None and not force:
                return existing

        name = (
            getattr(promise, "customer_name", None)
            or getattr(promise, "customer_id", None)
            or "Customer"
        )
        promise_date = getattr(promise, "promise_date", None)
        date_text = (
            promise_date.strftime("%d %b %Y, %I:%M %p")
            if promise_date is not None
            else "the promised date"
        )
        amount_value = float(getattr(promise, "promised_amount", 0) or 0)
        amount = f"₹{amount_value:,.2f}"

        if event_type == "PROMISE_CREATED":
            subject = "Revive: Promise-to-Pay recorded"
            body = (
                f"Hello {name},\n\n"
                f"Your Promise-to-Pay of {amount} is recorded for {date_text}."
            )
            if payment_link_url:
                body += (
                    "\n\nPay securely using your Revive payment link:\n"
                    f"{payment_link_url}"
                )

        elif event_type == "DUE_SOON":
            subject = "Revive: Your promised payment is due soon"
            body = (
                f"Hello {name},\n\n"
                f"Your promised payment of {amount} is due by {date_text}."
            )
            if payment_link_url:
                body += f"\n\nPay securely:\n{payment_link_url}"

        elif event_type == "PAYMENT_VERIFIED":
            subject = "Revive: Payment received and verified"
            body = (
                f"Hello {name},\n\n"
                f"Your payment of {amount} has been received and verified. "
                "Thank you."
            )

        else:
            subject = "Revive: Promise-to-Pay deadline missed"
            body = (
                f"Hello {name},\n\n"
                f"The Promise-to-Pay of {amount} was not verified by "
                f"{date_text}. Please contact the merchant if you need "
                "assistance."
            )

        if not recipient:
            with self._lock:
                return self._record(
                    idempotency_key=key,
                    promise_id=promise_id,
                    case_id=case_id,
                    channel="email",
                    event_type=event_type,
                    status="skipped",
                    reason="No customer email configured",
                )

        try:
            provider = self._send_email(recipient, subject, body)
            with self._lock:
                return self._record(
                    idempotency_key=key,
                    promise_id=promise_id,
                    case_id=case_id,
                    channel="email",
                    recipient=recipient,
                    event_type=event_type,
                    status="sent",
                    subject=subject,
                    **provider,
                )
        except Exception as exc:
            with self._lock:
                return self._record(
                    idempotency_key=key,
                    promise_id=promise_id,
                    case_id=case_id,
                    channel="email",
                    recipient=recipient,
                    event_type=event_type,
                    status="failed",
                    subject=subject,
                    reason=str(exc),
                )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._events[-max(1, int(limit)):]))

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "total": len(self._events),
                "sent": sum(
                    event.get("status") == "sent"
                    for event in self._events
                ),
                "failed": sum(
                    event.get("status") == "failed"
                    for event in self._events
                ),
                "skipped": sum(
                    event.get("status") == "skipped"
                    for event in self._events
                ),
            }

    def persistence_info(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "path": str(self.path),
                "exists": self.path.exists(),
                "event_count": len(self._events),
            }


customer_alert_service = CustomerAlertService()

# Backwards-compatible name for existing health/test commands.
customer_alert_store = customer_alert_service
