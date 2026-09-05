"""
Revive - Ops Copilot

An internal, tool-using chat assistant embedded in the dashboard.

Design constraints (deliberate):

    - This module never invents business logic. Every tool the
      model can call is a thin wrapper around an existing,
      already-tested dashboard_api function (the same functions
      the React dashboard itself calls). Nothing here recomputes
      ROI, policy, or recovery status.

    - Read tools (list cases, explain a decision, look up a
      customer, ...) execute immediately - they cannot change
      anything.

    - Write tools (create a promise, generate a payment link,
      mark paid, retry a payment, settle A2A, ...) are NEVER
      executed by the model directly. A call to a write tool is
      captured as a *pending action* and returned to the caller
      for a human to explicitly confirm or reject. Only the
      `/api/copilot/confirm` endpoint, driven by an operator
      clicking "Confirm" in the dashboard, actually invokes the
      underlying function.

    - Every executed write action is appended to an in-memory
      audit trail (`CopilotAgent.audit_log`) with who/what/when,
      so "what did the bot just do" is always answerable.

This module talks to Groq's OpenAI-compatible chat completions
endpoint. No OpenAI account or key is used anywhere - the `openai`
package is just a client library whose requests are pointed at
Groq's servers via `base_url`.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


# ============================================================
# Tool specification
# ============================================================


@dataclass(frozen=True)
class ToolSpec:
    """
    One tool the copilot can call.

    `handler` receives the parsed tool input (already validated
    against `input_schema`) and returns a plain JSON-serializable
    dict.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    mutating: bool = False
    confirmation_summary: Callable[[dict[str, Any]], str] | None = None

    def summarize(self, tool_input: dict[str, Any]) -> str:
        if self.confirmation_summary is not None:
            try:
                return self.confirmation_summary(tool_input)
            except Exception:
                pass
        return f"Call `{self.name}` with {tool_input!r}."


@dataclass
class PendingAction:
    action_id: str
    conversation_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    summary: str
    created_at: str


@dataclass
class AuditEntry:
    action_id: str
    conversation_id: str
    tool_name: str
    tool_input: dict[str, Any]
    approved: bool
    result: dict[str, Any]
    timestamp: str


SYSTEM_PROMPT = """You are the Revive Ops Copilot, an internal assistant embedded in the \
Revive revenue-recovery dashboard for the collections/ops team (NOT customer-facing).

Ground rules:
- Only use the tools provided. Never fabricate case IDs, amounts, dates, or statuses.
- Prefer calling a read tool over guessing, whenever a question depends on live data.
- Call at most ONE tool per turn. Never call more than one tool at once, and never call \
a write/mutating tool in the same turn as any other tool.
- Write tools (creating a promise, generating a payment link, marking paid/broken, \
retrying a payment, sending an alert, settling A2A) are never executed directly by you. \
Calling one of them will pause and ask a human operator to confirm - that is expected \
and correct behavior, not an error. After proposing one, briefly state what it will do \
and that it's awaiting confirmation.
- Be concise and concrete: cite case IDs, amounts, and dates rather than vague summaries.
- If a request is ambiguous (e.g. "the overdue one" with several matches), ask which \
case, or list the candidates via a read tool first.
- If something is outside what the tools can do (e.g. legal advice, changing policy \
rules), say so plainly rather than improvising.
"""


class CopilotAgent:
    def __init__(
        self,
        tools: list[ToolSpec],
        model: str = "openai/gpt-oss-20b",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tool_hops: int = 6,
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.model = model
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.base_url = base_url or os.getenv(
            "GROQ_BASE_URL", "https://api.groq.com/openai/v1"
        )
        self.max_tool_hops = max_tool_hops

        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self.pending_actions: dict[str, PendingAction] = {}
        self.audit_log: list[AuditEntry] = []

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def chat(self, conversation_id: str, user_message: str) -> dict[str, Any]:
        if not self.api_key:
            return {
                "reply": (
                    "The copilot isn't configured yet - GROQ_API_KEY is not set "
                    "on the backend. Everything else in Revive works fine without it; "
                    "set the key and restart the API to enable the copilot."
                ),
                "pending_action": None,
                "conversation_id": conversation_id,
            }

        history = self.conversations.setdefault(conversation_id, [])
        history.append({"role": "user", "content": user_message})

        return self._run_until_stop(conversation_id)

    def confirm(self, action_id: str, approved: bool) -> dict[str, Any]:
        pending = self.pending_actions.pop(action_id, None)

        if pending is None:
            return {
                "reply": "That action isn't pending anymore (it may have already been "
                "handled, or expired). Nothing changed.",
                "pending_action": None,
                "conversation_id": None,
            }

        tool = self.tools.get(pending.tool_name)
        history = self.conversations.setdefault(pending.conversation_id, [])

        if not approved:
            result: dict[str, Any] = {
                "declined": True,
                "message": "The operator declined this action. It was not performed.",
            }
        else:
            try:
                result = tool.handler(pending.tool_input) if tool else {
                    "error": f"Unknown tool {pending.tool_name!r}."
                }
            except Exception as exc:  # noqa: BLE001 - surface to the model/operator
                result = {"error": str(exc)}

        self.audit_log.append(
            AuditEntry(
                action_id=pending.action_id,
                conversation_id=pending.conversation_id,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
                approved=approved,
                result=result,
                timestamp=_now(),
            )
        )

        history.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": pending.tool_use_id,
                        "type": "function",
                        "function": {
                            "name": pending.tool_name,
                            "arguments": _to_text(pending.tool_input),
                        },
                    }
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": pending.tool_use_id,
                "content": _to_text(result),
            }
        )

        out = self._run_until_stop(pending.conversation_id)
        out["conversation_id"] = pending.conversation_id
        return out

    # --------------------------------------------------------
    # Internals
    # --------------------------------------------------------

    def _run_until_stop(self, conversation_id: str) -> dict[str, Any]:
        from openai import (
            APIConnectionError,
            APIError,
            APIStatusError,
            APITimeoutError,
            OpenAI,
            RateLimitError,
        )

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        history = self.conversations.setdefault(conversation_id, [])

        if not history or history[0].get("role") != "system":
            history.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in self.tools.values()
        ]

        for _ in range(self.max_tool_hops):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=history,
                    tools=tool_schemas,
                    tool_choice="auto",
                )

            except RateLimitError:
                return {
                    "reply": (
                        "Copilot is rate-limited right now. "
                        "Please try again in a moment."
                    ),
                    "pending_action": None,
                    "conversation_id": conversation_id,
                }

            except APITimeoutError:
                return {
                    "reply": (
                        "Copilot timed out reaching the model "
                        "provider. Please try again."
                    ),
                    "pending_action": None,
                    "conversation_id": conversation_id,
                }

            except (
                APIConnectionError,
                APIStatusError,
                APIError,
            ) as exc:
                return {
                    "reply": (
                        "Copilot is temporarily unavailable "
                        "(model provider error). Please try "
                        "again shortly."
                    ),
                    "pending_action": None,
                    "conversation_id": conversation_id,
                    "error": str(exc),
                }

            message = response.choices[0].message
            history.append(_message_to_input(message))

            if not message.tool_calls:
                text = (message.content or "").strip()
                return {
                    "reply": text or "(no response)",
                    "pending_action": None,
                    "conversation_id": conversation_id,
                }

            preceding_text = (message.content or "").strip()

            pending_this_turn: PendingAction | None = None
            tool_results: list[dict[str, Any]] = []

            for call in message.tool_calls:
                tool = self.tools.get(call.function.name)
                try:
                    tool_input = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_input = {}

                if tool is None:
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _to_text(
                                {"error": f"Unknown tool {call.function.name!r}."}
                            ),
                        }
                    )
                    continue

                if tool.mutating:
                    if pending_this_turn is None:
                        action_id = uuid.uuid4().hex[:12]
                        pending_this_turn = PendingAction(
                            action_id=action_id,
                            conversation_id=conversation_id,
                            tool_name=tool.name,
                            tool_input=dict(tool_input),
                            tool_use_id=call.id,
                            summary=tool.summarize(tool_input),
                            created_at=_now(),
                        )
                        self.pending_actions[action_id] = pending_this_turn
                    else:
                        # A second write tool in the same turn: don't execute it,
                        # and don't leave it hanging either.
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": _to_text(
                                    {
                                        "skipped": True,
                                        "message": (
                                            "Skipped: only one pending action is allowed "
                                            "per turn. Ask the operator about this "
                                            "separately once the first action is resolved."
                                        ),
                                    }
                                ),
                            }
                        )
                    continue

                try:
                    result = tool.handler(tool_input)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)}

                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": _to_text(result),
                    }
                )

            if pending_this_turn is not None:
                # Satisfy the API contract for any tool calls we didn't
                # execute yet (read tools called alongside the write tool),
                # then pause and hand control back to the operator. We
                # DON'T include the mutating tool's result yet, so the
                # next call would still be missing one - stop here instead
                # of looping again.
                history.extend(tool_results)
                reply = preceding_text or (
                    f"I'd like to run **{pending_this_turn.tool_name}** "
                    f"({pending_this_turn.summary}). Confirm to proceed?"
                )
                return {
                    "reply": reply,
                    "pending_action": {
                        "action_id": pending_this_turn.action_id,
                        "tool_name": pending_this_turn.tool_name,
                        "tool_input": pending_this_turn.tool_input,
                        "summary": pending_this_turn.summary,
                    },
                    "conversation_id": conversation_id,
                }

            history.extend(tool_results)
            # loop again with tool results in context

        return {
            "reply": "I made a few tool calls but couldn't wrap up cleanly - could you "
            "rephrase or narrow the request?",
            "pending_action": None,
            "conversation_id": conversation_id,
        }


# ============================================================
# Helpers
# ============================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_text(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, default=str)[:8000]
    except Exception:
        return str(payload)[:8000]


def _message_to_input(message: Any) -> dict[str, Any]:
    """Convert an OpenAI SDK assistant message object back into a plain
    dict so the history list stays JSON-serializable and replayable."""

    out: dict[str, Any] = {"role": "assistant", "content": message.content}

    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]

    return out