"""
Independent Payer/AP Agent

This service is intentionally separate from Revive's
merchant settlement engine.

It exposes an A2A 1.0 JSON-RPC endpoint.

For production:
    Replace the deterministic decision logic with the
    payer organization's real AP/ERP authorization system.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request


# ============================================================
# Application
# ============================================================

app = FastAPI(
    title="Revive Demo Payer AP Agent",
    version="1.0.0",
)


# ============================================================
# Configuration
# ============================================================

A2A_TOKEN = os.getenv(
    "A2A_PAYER_AGENT_BEARER_TOKEN",
    "change-me",
)

AGENT_ID = os.getenv(
    "PAYER_AGENT_ID",
    "payer-ap-agent-demo-001",
)

# IMPORTANT:
# This is the URL advertised to the calling agent.
#
# Local browser testing:
#     http://127.0.0.1:8100
#
# Revive backend running inside Docker:
#     http://host.docker.internal:8100
#
A2A_PUBLIC_URL = os.getenv(
    "A2A_PUBLIC_URL",
    "http://127.0.0.1:8100",
).rstrip("/")


# ============================================================
# Agent Card
# ============================================================

@app.get("/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    """
    Publish the A2A Agent Card.

    Revive discovers this endpoint first and then uses the
    advertised JSON-RPC interface for settlement negotiation.
    """

    return {
        "name": "Independent Payer AP Agent",
        "description": (
            "Autonomous accounts-payable agent that "
            "negotiates bounded invoice settlements."
        ),
        "id": AGENT_ID,
        "protocolVersion": "1.0",
        "supportedInterfaces": [
            {
                "url": f"{A2A_PUBLIC_URL}/rpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": [
            "text/plain",
        ],
        "defaultOutputModes": [
            "text/plain",
        ],
        "skills": [
            {
                "id": "invoice-settlement",
                "name": "Invoice Settlement",
                "description": (
                    "Negotiates B2B invoice settlement "
                    "within payer authorization limits."
                ),
                "tags": [
                    "invoice",
                    "settlement",
                    "accounts-payable",
                ],
                "inputModes": [
                    "text/plain",
                ],
                "outputModes": [
                    "text/plain",
                ],
            }
        ],
    }


# ============================================================
# Authentication
# ============================================================

def verify_token(
    authorization: str | None,
) -> None:
    """
    Verify the bearer token supplied by the calling agent.
    """

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header.",
        )

    expected = f"Bearer {A2A_TOKEN}"

    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer token.",
        )


# ============================================================
# JSON-RPC helpers
# ============================================================

def jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
) -> dict[str, Any]:
    """
    Build a JSON-RPC 2.0 error response.
    """

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def jsonrpc_success(
    request_id: Any,
    result: dict[str, Any],
) -> dict[str, Any]:
    """
    Build a JSON-RPC 2.0 success response.
    """

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


# ============================================================
# Extract settlement payload
# ============================================================

def extract_settlement_payload(
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Extract the JSON settlement contract from A2A message parts.

    Revive sends the settlement contract as a JSON string inside
    a text/plain A2A Part.
    """

    parts = message.get("parts", [])

    if not isinstance(parts, list):
        return None

    for part in parts:
        if not isinstance(part, dict):
            continue

        text = part.get("text")

        if not isinstance(text, str):
            continue

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            return parsed

    return None


# ============================================================
# A2A JSON-RPC endpoint
# ============================================================

@app.post("/rpc")
async def rpc(
    request: Request,
    authorization: str | None = Header(
        default=None,
    ),
) -> dict[str, Any]:

    # --------------------------------------------------------
    # Authentication
    # --------------------------------------------------------

    verify_token(authorization)

    # --------------------------------------------------------
    # Parse request
    # --------------------------------------------------------

    try:
        body = await request.json()
    except Exception:
        return jsonrpc_error(
            None,
            -32700,
            "Invalid JSON.",
        )

    if not isinstance(body, dict):
        return jsonrpc_error(
            None,
            -32600,
            "Invalid JSON-RPC request.",
        )

    request_id = body.get("id")

    # --------------------------------------------------------
    # JSON-RPC validation
    # --------------------------------------------------------

    if body.get("jsonrpc") != "2.0":
        return jsonrpc_error(
            request_id,
            -32600,
            "Invalid JSON-RPC request.",
        )

    method = body.get("method")

    # Current Revive A2A contract uses message/send.
    if method != "message/send":
        return jsonrpc_error(
            request_id,
            -32601,
            "Method not found.",
        )

    params = body.get("params", {})

    if not isinstance(params, dict):
        return jsonrpc_error(
            request_id,
            -32602,
            "Invalid params.",
        )

    message = params.get("message", {})

    if not isinstance(message, dict):
        return jsonrpc_error(
            request_id,
            -32602,
            "Invalid message.",
        )

    # --------------------------------------------------------
    # Extract settlement contract
    # --------------------------------------------------------

    payload = extract_settlement_payload(message)

    if payload is None:
        return jsonrpc_error(
            request_id,
            -32602,
            "Missing Revive settlement payload.",
        )

    # --------------------------------------------------------
    # Contract validation
    # --------------------------------------------------------

    if (
        payload.get("contract_version")
        != "revive.settlement.v1"
    ):
        return jsonrpc_error(
            request_id,
            -32602,
            "Unsupported settlement contract.",
        )

    # --------------------------------------------------------
    # Required invoice validation
    # --------------------------------------------------------

    invoice_id = payload.get("invoice_id")

    if not isinstance(invoice_id, str) or not invoice_id.strip():
        return jsonrpc_error(
            request_id,
            -32602,
            "Missing invoice_id.",
        )

    # --------------------------------------------------------
    # Monetary validation
    # --------------------------------------------------------

    try:
        original_amount = Decimal(
            str(
                payload.get(
                    "original_amount",
                    "0",
                )
            )
        )

        proposed_amount = Decimal(
            str(
                payload.get(
                    "amount",
                    "0",
                )
            )
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):
        return jsonrpc_error(
            request_id,
            -32602,
            "Invalid monetary amount.",
        )

    # Reject NaN / Infinity-like values.
    if (
        not original_amount.is_finite()
        or not proposed_amount.is_finite()
    ):
        return jsonrpc_error(
            request_id,
            -32602,
            "Invalid monetary amount.",
        )

    # --------------------------------------------------------
    # Payer decision
    # --------------------------------------------------------

    if (
        original_amount <= 0
        or proposed_amount <= 0
        or proposed_amount > original_amount
    ):
        decision = "REJECTED"

        amount = proposed_amount

        message_text = (
            "Payer AP agent rejected the proposal "
            "because the monetary values are invalid."
        )

    # --------------------------------------------------------
    # Autonomous payer authorization boundary
    # --------------------------------------------------------
    #
    # Demo payer policy:
    #
    # Maximum autonomous discount = 5%
    #
    # Therefore:
    #
    # proposed >= 95% of original
    #
    # is automatically accepted.
    #
    # Anything below that requires human/ERP authorization
    # and is rejected by this autonomous demo agent.
    # --------------------------------------------------------

    elif proposed_amount >= (
        original_amount * Decimal("0.95")
    ):
        decision = "ACCEPTED"

        amount = proposed_amount

        message_text = (
            "Payer AP agent accepted the proposal "
            "within its autonomous authorization boundary."
        )

    else:
        decision = "REJECTED"

        amount = proposed_amount

        message_text = (
            "Payer AP agent rejected the proposal because "
            "the requested discount exceeds its autonomous "
            "authorization limit."
        )

    # --------------------------------------------------------
    # Build settlement response
    # --------------------------------------------------------

    settlement_response = {
        "contract_version": "revive.settlement.v1",
        "invoice_id": invoice_id,
        "decision": decision,
        "amount": f"{amount:.2f}",
        "message": message_text,
        "agent_id": AGENT_ID,
    }

    # --------------------------------------------------------
    # Build A2A agent message
    # --------------------------------------------------------

    response_message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_AGENT",
        "parts": [
            {
                "text": json.dumps(
                    settlement_response,
                    separators=(
                        ",",
                        ":",
                    ),
                ),
            }
        ],
    }

    # --------------------------------------------------------
    # JSON-RPC response
    # --------------------------------------------------------

    return jsonrpc_success(
        request_id,
        {
            "message": response_message,
        },
    )