"""Structured errors across the MCP boundary.

The MCP whitepaper (§"Error Handling") distinguishes:
- **protocol errors** (JSON-RPC): unknown tool, invalid arguments — the SDK emits these;
- **tool errors** (`isError: true`): the tool ran but the *operation* failed —
  backend unavailable, business rule, wrong state.

The message channel is for the calling model: it must say what happened *and* what
to do next. Every ToolError therefore carries a code, a human sentence and a hint.
"""
from __future__ import annotations

import json
from typing import Any

from rhythm.agent.registry import ToolError

RETRY_HINT = {
    "wrong_state": "Fix the day's state first (e.g. run rhythm.checkin), then retry.",
    "idempotency_key_required": "Generate a stable key for this intent and retry with it.",
    "invalid_arguments": "Correct the fields named in the message, then retry.",
    "not_found": "Check the identifier; list first if unsure.",
    "forbidden": "Do not retry with the same credential.",
}


def as_tool_message(exc: ToolError) -> str:
    """One message, three channels: what, why, what next."""
    parts = [f"[{exc.code}] {exc.message}"]
    if exc.hint:
        parts.append(f"Next step: {exc.hint}")
    if exc.retryable:
        parts.append("This failure is retryable.")
    return " ".join(parts)


def structured_content(exc: ToolError) -> str:
    """Machine-readable twin of the message, for clients that parse."""
    return json.dumps({"error": exc.as_dict()}, ensure_ascii=False)
