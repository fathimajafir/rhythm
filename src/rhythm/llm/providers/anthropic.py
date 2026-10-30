"""Anthropic Messages API adapter, with thinking-block round-trip.

The rule that matters: when tools are in play and thinking is enabled, the
assistant's `thinking` block **must be returned verbatim** (signature included) on
the next turn. Dropping it is an API error; truncating it invalidates the
signature. This adapter therefore preserves `ReasoningPart(signature=...)`
exactly as received.

Thinking mode also constrains sampling: temperature must stay at the default, so
the compiler omits it and this adapter never sets one while thinking is active.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from ..capabilities import ModelSpec, ReasoningStyle
from ..compiler import Compiled
from ..messages import (
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from .base import ProviderError

FINISH_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.anthropic.com/v1",
        version: str = "2023-06-01",
        timeout_s: float = 90.0,
        max_retries: int = 2,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._transport = transport

    # ------------------------------------------------------------------ wire
    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
        }

    def _system(self, req: LLMRequest) -> Optional[str]:
        blocks = [p.text for m in req.messages if m.role is Role.SYSTEM for p in m.parts
                  if isinstance(p, TextPart)]
        return "\n\n".join(blocks) or None

    def _messages(self, req: LLMRequest, spec: ModelSpec) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in req.messages:
            if msg.role is Role.SYSTEM:
                continue

            if msg.role is Role.TOOL:
                part = msg.parts[0]
                assert isinstance(part, ToolResultPart)
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": part.call_id,
                                "content": part.content,
                                **({"is_error": True} if part.is_error else {}),
                            }
                        ],
                    }
                )
                continue

            blocks: list[dict[str, Any]] = []
            for part in msg.parts:
                if isinstance(part, ReasoningPart):
                    # Round-trip only what the provider signed; anything else
                    # (imported from another vendor) must not be sent.
                    if part.signature and not part.redacted:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": part.text,
                                "signature": part.signature,
                            }
                        )
                elif isinstance(part, ToolCallPart):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": part.id,
                            "name": part.name,
                            "input": part.arguments,
                        }
                    )
                elif isinstance(part, TextPart) and part.text:
                    blocks.append({"type": "text", "text": part.text})

            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            out.append({"role": "assistant" if msg.role is Role.ASSISTANT else "user", "content": blocks})
        return out

    def _payload(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": spec.id.split("/", 1)[-1],
            "messages": self._messages(req, spec),
            "max_tokens": compiled.max_output_tokens,
        }
        system = self._system(req)
        if system:
            payload["system"] = system

        thinking_active = (
            compiled.reasoning_style is ReasoningStyle.BUDGET
            and (compiled.reasoning_budget or 0) > 0
        )
        if thinking_active:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": compiled.reasoning_budget,
            }
            # max_tokens must exceed the thinking budget.
            if payload["max_tokens"] <= compiled.reasoning_budget:
                payload["max_tokens"] = compiled.reasoning_budget + 1024

        if compiled.temperature is not None and not thinking_active:
            payload["temperature"] = compiled.temperature
        if compiled.stop:
            payload["stop_sequences"] = list(compiled.stop)

        if compiled.tools_enabled:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in req.tools
            ]
        return payload

    # ------------------------------------------------------------------ call
    def complete(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> LLMResponse:
        payload = self._payload(req, spec, compiled)
        url = f"{self.base_url}/messages"

        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    transport=self._transport,
                    timeout=min(self.timeout_s, req.timeout_s),
                ) as client:
                    resp = client.post(url, json=payload, headers=self._headers())
                if resp.status_code in (429, 500, 502, 503, 529):
                    if attempt < self.max_retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise ProviderError(
                        "provider_unavailable", str(resp.status_code), retryable=True,
                        status=resp.status_code, provider=self.name,
                    )
                if resp.status_code >= 400:
                    raise ProviderError(
                        "provider_rejected", f"{resp.status_code}: {resp.text[:300]}",
                        status=resp.status_code, provider=self.name,
                    )
                return self._parse(resp.json(), spec)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ProviderError("network", str(exc), retryable=True, provider=self.name)
        raise ProviderError("unknown", "unreachable", provider=self.name)

    # ------------------------------------------------------------------ parse
    def _parse(self, body: dict[str, Any], spec: ModelSpec) -> LLMResponse:
        parts: list = []
        for block in body.get("content", []):
            btype = block.get("type")
            if btype == "text":
                if block.get("text"):
                    parts.append(TextPart(block["text"]))
            elif btype == "thinking":
                parts.append(
                    ReasoningPart(
                        text=block.get("thinking", ""),
                        signature=block.get("signature"),
                        redacted=block.get("type") == "redacted_thinking",
                    )
                )
            elif btype == "redacted_thinking":
                parts.append(
                    ReasoningPart(
                        text="",
                        opaque=json.dumps(block),
                        redacted=True,
                    )
                )
            elif btype == "tool_use":
                parts.append(
                    ToolCallPart(id=block["id"], name=block["name"], arguments=block.get("input") or {})
                )

        usage = body.get("usage", {}) or {}
        return LLMResponse(
            message=Message(Role.ASSISTANT, tuple(parts)),
            model=body.get("model", spec.id),
            provider=self.name,
            finish_reason=FINISH_MAP.get(body.get("stop_reason", "end_turn"), "stop"),
            usage=Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                reasoning_tokens=0,  # billed within output_tokens
                cached_input_tokens=(usage.get("cache_read_input_tokens") or 0),
            ),
            request_id=body.get("id", ""),
        )
