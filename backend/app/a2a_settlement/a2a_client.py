"""
Revive A2A HTTP Client

Client used by the Revive merchant agent to communicate with
an independent payer/AP agent over A2A 1.0 JSON-RPC.

Responsibilities:

    1. Discover the payer Agent Card.
    2. Validate the advertised JSON-RPC interface.
    3. Send a Revive settlement contract.
    4. Authenticate using Bearer authentication.
    5. Parse the A2A message response.
    6. Extract the Revive settlement decision.
    7. Fail safely on malformed/untrusted responses.

A2A communication is NOT payment confirmation.

An accepted settlement means:

    AGREED

It does not mean:

    PAYMENT_CONFIRMED
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ============================================================
# Exceptions
# ============================================================


class A2AClientError(RuntimeError):
    """
    Raised when A2A communication or response validation fails.
    """

    pass


# ============================================================
# Data structures
# ============================================================


@dataclass(frozen=True)
class AgentInterface:
    """
    One advertised A2A interface from the Agent Card.
    """

    url: str
    protocol_binding: str
    protocol_version: str


@dataclass(frozen=True)
class AgentCard:
    """
    Minimal validated representation of an A2A Agent Card.
    """

    name: str
    agent_id: str
    protocol_version: str
    interfaces: list[AgentInterface]


@dataclass(frozen=True)
class PayerSettlementResponse:
    """
    Validated business-level response from the payer agent.
    """

    decision: str
    amount: Decimal
    message: str
    invoice_id: str
    agent_id: str

    # A2A metadata where available.
    message_id: str | None = None
    task_id: str | None = None
    context_id: str | None = None


# ============================================================
# HTTP helpers
# ============================================================


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    bearer_token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:

    headers = {
        "Accept": "application/json",
    }

    data: bytes | None = None

    if payload is not None:

        data = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")

        headers["Content-Type"] = (
            "application/json"
        )

    if bearer_token:

        headers["Authorization"] = (
            f"Bearer {bearer_token}"
        )

    request = Request(
        url=url,
        data=data,
        headers=headers,
        method=method,
    )

    try:

        with urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw = response.read()

    except HTTPError as exc:

        try:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            body = ""

        raise A2AClientError(
            f"A2A HTTP {exc.code} from {url}: {body[:500]}"
        ) from exc

    except URLError as exc:

        raise A2AClientError(
            f"Unable to reach A2A endpoint {url}: {exc}"
        ) from exc

    except TimeoutError as exc:

        raise A2AClientError(
            f"A2A request timed out: {url}"
        ) from exc

    except Exception as exc:

        raise A2AClientError(
            f"A2A HTTP request failed for {url}: {exc}"
        ) from exc

    try:

        parsed = json.loads(
            raw.decode(
                "utf-8"
            )
        )

    except Exception as exc:

        raise A2AClientError(
            "A2A endpoint returned invalid JSON."
        ) from exc

    if not isinstance(
        parsed,
        dict,
    ):

        raise A2AClientError(
            "A2A endpoint returned a non-object JSON response."
        )

    return parsed


# ============================================================
# Agent Card parsing
# ============================================================


def _parse_agent_card(
    payload: dict[str, Any],
) -> AgentCard:

    name = payload.get(
        "name"
    )

    agent_id = payload.get(
        "id"
    )

    protocol_version = payload.get(
        "protocolVersion"
    )

    if not isinstance(
        name,
        str,
    ) or not name.strip():

        raise A2AClientError(
            "A2A Agent Card is missing a valid name."
        )

    if not isinstance(
        agent_id,
        str,
    ) or not agent_id.strip():

        raise A2AClientError(
            "A2A Agent Card is missing a valid agent id."
        )

    if not isinstance(
        protocol_version,
        str,
    ) or not protocol_version.strip():

        raise A2AClientError(
            "A2A Agent Card is missing protocolVersion."
        )

    raw_interfaces = payload.get(
        "supportedInterfaces",
        [],
    )

    if not isinstance(
        raw_interfaces,
        list,
    ):

        raise A2AClientError(
            "A2A Agent Card has invalid supportedInterfaces."
        )

    interfaces: list[
        AgentInterface
    ] = []

    for item in raw_interfaces:

        if not isinstance(
            item,
            dict,
        ):
            continue

        url = item.get(
            "url"
        )

        protocol_binding = item.get(
            "protocolBinding"
        )

        interface_protocol_version = item.get(
            "protocolVersion",
            protocol_version,
        )

        if not isinstance(
            url,
            str,
        ) or not url.strip():

            continue

        if not isinstance(
            protocol_binding,
            str,
        ):

            continue

        interfaces.append(
            AgentInterface(
                url=url,
                protocol_binding=(
                    protocol_binding
                ),
                protocol_version=(
                    str(
                        interface_protocol_version
                    )
                ),
            )
        )

    if not interfaces:

        raise A2AClientError(
            "A2A Agent Card does not advertise a usable interface."
        )

    return AgentCard(
        name=name,
        agent_id=agent_id,
        protocol_version=protocol_version,
        interfaces=interfaces,
    )


# ============================================================
# Response extraction
# ============================================================


def _extract_text_parts(
    message: dict[str, Any],
) -> list[str]:
    """
    Extract text values from A2A message parts.

    Supports the current text representation used by the
    Revive reference payer agent.
    """

    parts = message.get(
        "parts",
        [],
    )

    if not isinstance(
        parts,
        list,
    ):

        return []

    texts: list[str] = []

    for part in parts:

        if not isinstance(
            part,
            dict,
        ):
            continue

        text = part.get(
            "text"
        )

        if isinstance(
            text,
            str,
        ):

            texts.append(
                text
            )

    return texts


def _find_settlement_payload(
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Find the Revive settlement JSON embedded in an A2A message.

    Expected:

        result.message.parts[].text

    where text contains:

        {
            "contract_version": "revive.settlement.v1",
            ...
        }
    """

    for text in _extract_text_parts(
        message
    ):

        try:

            parsed = json.loads(
                text
            )

        except json.JSONDecodeError:

            continue

        if not isinstance(
            parsed,
            dict,
        ):

            continue

        if (
            parsed.get(
                "contract_version"
            )
            == "revive.settlement.v1"
        ):

            return parsed

    return None


def _parse_settlement_response(
    response: dict[str, Any],
    *,
    expected_invoice_id: str | None = None,
) -> PayerSettlementResponse:
    """
    Parse the complete JSON-RPC → A2A → Revive response.

    Expected shape:

        {
          "jsonrpc": "2.0",
          "id": "...",
          "result": {
            "message": {
              "messageId": "...",
              "role": "ROLE_AGENT",
              "parts": [
                {
                  "text": "{...Revive settlement...}"
                }
              ]
            }
          }
        }
    """

    # --------------------------------------------------------
    # JSON-RPC error
    # --------------------------------------------------------

    if "error" in response:

        error = response.get(
            "error"
        )

        if isinstance(
            error,
            dict,
        ):

            code = error.get(
                "code",
                "unknown",
            )

            message = error.get(
                "message",
                "Unknown A2A error.",
            )

            raise A2AClientError(
                f"Payer A2A JSON-RPC error "
                f"{code}: {message}"
            )

        raise A2AClientError(
            "Payer A2A returned a JSON-RPC error."
        )

    # --------------------------------------------------------
    # JSON-RPC result
    # --------------------------------------------------------

    result = response.get(
        "result"
    )

    if not isinstance(
        result,
        dict,
    ):

        raise A2AClientError(
            "Payer A2A response does not contain a valid result."
        )

    # --------------------------------------------------------
    # A2A message
    # --------------------------------------------------------

    message = result.get(
        "message"
    )

    if not isinstance(
        message,
        dict,
    ):

        raise A2AClientError(
            "Payer A2A result does not contain a message."
        )

    # --------------------------------------------------------
    # Message metadata
    # --------------------------------------------------------

    message_id = message.get(
        "messageId"
    )

    if not isinstance(
        message_id,
        str,
    ):

        message_id = None

    task_id = result.get(
        "taskId"
    )

    if not isinstance(
        task_id,
        str,
    ):

        task_id = None

    context_id = result.get(
        "contextId"
    )

    if not isinstance(
        context_id,
        str,
    ):

        context_id = None

    # --------------------------------------------------------
    # Revive settlement payload
    # --------------------------------------------------------

    settlement = (
        _find_settlement_payload(
            message
        )
    )

    if settlement is None:

        raise A2AClientError(
            "Payer A2A response did not contain "
            "a Revive settlement decision."
        )

    # --------------------------------------------------------
    # Contract version
    # --------------------------------------------------------

    if (
        settlement.get(
            "contract_version"
        )
        != "revive.settlement.v1"
    ):

        raise A2AClientError(
            "Payer A2A response used an unsupported "
            "Revive settlement contract."
        )

    # --------------------------------------------------------
    # Decision
    # --------------------------------------------------------

    decision = settlement.get(
        "decision"
    )

    if decision not in {
        "ACCEPTED",
        "COUNTER_OFFER",
        "REJECTED",
    }:

        raise A2AClientError(
            "Payer A2A response contains an invalid settlement decision."
        )

    # --------------------------------------------------------
    # Amount
    # --------------------------------------------------------

    raw_amount = settlement.get(
        "amount"
    )

    try:

        amount = Decimal(
            str(raw_amount)
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ) as exc:

        raise A2AClientError(
            "Payer A2A response contains an invalid amount."
        ) from exc

    if not amount.is_finite():

        raise A2AClientError(
            "Payer A2A response contains a non-finite amount."
        )

    if amount < 0:

        raise A2AClientError(
            "Payer A2A response contains a negative amount."
        )

    # --------------------------------------------------------
    # Invoice ID
    # --------------------------------------------------------

    invoice_id = settlement.get(
        "invoice_id"
    )

    if not isinstance(
        invoice_id,
        str,
    ) or not invoice_id.strip():

        raise A2AClientError(
            "Payer A2A response is missing invoice_id."
        )

    # Prevent a response for a different invoice from being
    # accidentally associated with the current settlement.
    if (
        expected_invoice_id is not None
        and invoice_id
        != expected_invoice_id
    ):

        raise A2AClientError(
            "Payer A2A response invoice_id does not "
            "match the requested invoice."
        )

    # --------------------------------------------------------
    # Agent ID
    # --------------------------------------------------------

    agent_id = settlement.get(
        "agent_id"
    )

    if not isinstance(
        agent_id,
        str,
    ) or not agent_id.strip():

        raise A2AClientError(
            "Payer A2A response is missing agent_id."
        )

    # --------------------------------------------------------
    # Message
    # --------------------------------------------------------

    message_text = settlement.get(
        "message",
        "",
    )

    if not isinstance(
        message_text,
        str,
    ):

        message_text = str(
            message_text
        )

    return PayerSettlementResponse(
        decision=decision,
        amount=amount,
        message=message_text,
        invoice_id=invoice_id,
        agent_id=agent_id,
        message_id=message_id,
        task_id=task_id,
        context_id=context_id,
    )


# ============================================================
# HTTP A2A payer client
# ============================================================


class HttpA2APayerAgentClient:
    """
    HTTP JSON-RPC A2A client for an independent payer agent.
    """

    def __init__(
        self,
        agent_card_url: str,
        bearer_token: str,
        timeout: float = 10.0,
    ) -> None:

        if not agent_card_url.strip():

            raise A2AClientError(
                "A2A Agent Card URL is empty."
            )

        if not bearer_token.strip():

            raise A2AClientError(
                "A2A bearer token is empty."
            )

        self.agent_card_url = (
            agent_card_url.rstrip("/")
        )

        self.bearer_token = (
            bearer_token
        )

        self.timeout = float(
            timeout
        )

        self.card = (
            self._discover_agent()
        )

    # ========================================================
    # Discovery
    # ========================================================

    def _discover_agent(
        self,
    ) -> AgentCard:

        payload = _http_json(
            self.agent_card_url,
            method="GET",
            timeout=self.timeout,
        )

        card = _parse_agent_card(
            payload
        )

        return card

    # ========================================================
    # Interface selection
    # ========================================================

    def _rpc_url(
        self,
    ) -> str:

        for interface in self.card.interfaces:

            if (
                interface.protocol_binding
                == "JSONRPC"
            ):

                return interface.url

        raise A2AClientError(
            "Payer Agent Card does not advertise "
            "a JSONRPC interface."
        )

    # ========================================================
    # Settlement request
    # ========================================================

    def send_settlement(
        self,
        settlement_payload: dict[str, Any],
    ) -> PayerSettlementResponse:
        """
        Send one Revive settlement proposal through A2A.
        """

        invoice_id = settlement_payload.get(
            "invoice_id"
        )

        if not isinstance(
            invoice_id,
            str,
        ):

            raise A2AClientError(
                "Settlement payload is missing invoice_id."
            )

        # ----------------------------------------------------
        # A2A message
        # ----------------------------------------------------

        message = {
            "messageId": str(
                uuid.uuid4()
            ),
            "role": "ROLE_USER",
            "parts": [
                {
                    "text": json.dumps(
                        settlement_payload,
                        separators=(
                            ",",
                            ":",
                        ),
                    )
                }
            ],
        }

        # ----------------------------------------------------
        # JSON-RPC request
        # ----------------------------------------------------

        rpc_request = {
            "jsonrpc": "2.0",
            "id": str(
                uuid.uuid4()
            ),
            "method": "message/send",
            "params": {
                "message": message,
            },
        }

        # ----------------------------------------------------
        # Send
        # ----------------------------------------------------

        response = _http_json(
            self._rpc_url(),
            method="POST",
            payload=rpc_request,
            bearer_token=self.bearer_token,
            timeout=self.timeout,
        )

        # ----------------------------------------------------
        # Parse and validate
        # ----------------------------------------------------

        return _parse_settlement_response(
            response,
            expected_invoice_id=invoice_id,
        )


# ============================================================
# Environment configuration
# ============================================================


def build_remote_payer_client(
) -> HttpA2APayerAgentClient | None:
    """
    Build the configured remote payer client.

    If A2A_PAYER_AGENT_CARD_URL is not configured,
    return None so the existing offline/mock path can remain
    available for benchmark testing.
    """

    card_url = os.getenv(
        "A2A_PAYER_AGENT_CARD_URL",
        "",
    ).strip()

    if not card_url:

        return None

    token = os.getenv(
        "A2A_PAYER_AGENT_BEARER_TOKEN",
        "",
    ).strip()

    if not token:

        raise A2AClientError(
            "A2A_PAYER_AGENT_BEARER_TOKEN is required "
            "when A2A_PAYER_AGENT_CARD_URL is configured."
        )

    raw_timeout = os.getenv(
        "A2A_REQUEST_TIMEOUT_SECONDS",
        "10",
    )

    try:

        timeout = float(
            raw_timeout
        )

    except ValueError as exc:

        raise A2AClientError(
            "A2A_REQUEST_TIMEOUT_SECONDS must be numeric."
        ) from exc

    if timeout <= 0:

        raise A2AClientError(
            "A2A_REQUEST_TIMEOUT_SECONDS must be greater than zero."
        )

    return HttpA2APayerAgentClient(
        agent_card_url=card_url,
        bearer_token=token,
        timeout=timeout,
    )