"""Structured output with a model-agnostic validate-and-repair loop.

Native JSON-schema support varies wildly (reasoning models especially), so the
strategy comes from the capability spec (§ compiler) and the *guarantee* comes
from here: whatever the model returns, the caller either gets schema-valid JSON
or an exception — never plausible-looking garbage.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from .messages import LLMRequest, Message, Role, TextPart, ToolSchema
from .router import Router

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredOutputError(RuntimeError):
    def __init__(self, message: str, *, attempts: int, last_errors: list[str]) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_errors = last_errors


@dataclass
class StructuredResult:
    value: dict[str, Any]
    attempts: int
    model: str
    reasoning_text: str = ""  # kept for the trace, never for the user


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    fenced = FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    # Tolerate prose around the object: take the outermost {...}.
    start, end = candidate.find("{"), candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def request_structured(
    router: Router,
    *,
    task: str,
    schema: dict[str, Any],
    messages: list[Message],
    tools: tuple[ToolSchema, ...] = (),
    max_output_tokens: int = 800,
    max_repairs: int = 2,
    reasoning_budget_tokens: Optional[int] = None,
) -> StructuredResult:
    """Ask, validate, and repair. The schema is the contract, not the model's mood."""
    from ..jsonschema_lite import validate  # local import avoids a cycle at module load

    conversation = list(messages)
    attempts = 0
    last_errors: list[str] = []
    model = ""
    reasoning_text = ""

    for attempt in range(max_repairs + 1):
        attempts += 1
        req = LLMRequest(
            messages=tuple(conversation),
            tools=tools,
            response_schema=schema,
            max_output_tokens=max_output_tokens,
            reasoning_budget_tokens=reasoning_budget_tokens,
        )
        response = router.complete(task, req)
        model = response.model
        reasoning = response.reasoning
        if reasoning is not None and reasoning.text:
            reasoning_text = reasoning.text

        if response.tool_calls:
            # A tool call is not an answer; surface it as an error so callers
            # can decide (the agent loop handles tools itself).
            raise StructuredOutputError(
                f"model returned tool calls instead of JSON: "
                f"{[c.name for c in response.tool_calls]}",
                attempts=attempts,
                last_errors=["tool_calls_instead_of_json"],
            )

        value = _extract_json(response.text)
        if value is None:
            last_errors = ["response was not parseable JSON"]
        else:
            last_errors = validate(schema, value)
            if not last_errors:
                return StructuredResult(
                    value=value, attempts=attempts, model=model, reasoning_text=reasoning_text
                )

        if attempt < max_repairs:
            conversation.append(Message(Role.ASSISTANT, (TextPart(response.text),)))
            conversation.append(
                Message(
                    Role.USER,
                    (
                        TextPart(
                            "Your response did not satisfy the schema.\n"
                            f"Errors:\n- " + "\n- ".join(last_errors) +
                            "\nReturn ONLY corrected JSON conforming to the schema. No prose."
                        ),
                    ),
                )
            )

    raise StructuredOutputError(
        f"schema not satisfied after {attempts} attempts: {'; '.join(last_errors[:5])}",
        attempts=attempts,
        last_errors=last_errors,
    )
