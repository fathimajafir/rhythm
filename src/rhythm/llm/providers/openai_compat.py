"""OpenAI-compatible adapter: OpenAI, vLLM, SGLang, Ollama, Together, Groq, OpenRouter.

One adapter covers most of the market because they share the chat-completions wire
format. Differences (reasoning field names, max_tokens naming) are handled via the
capability spec, not by if-branching on hosts.

Round-trip rules honoured here:
- `Roundtrip.FORBIDDEN` (DeepSeek/Qwen): `reasoning_content` is extracted but never
  echoed back — these APIs reject it on input;
- `Roundtrip.OPAQUE` (OpenAI reasoning models): the encrypted reasoning item is
  passed back so a stateless tool loop still "remembers" its thinking.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from ..capabilities import ModelSpec, ReasoningStyle, Roundtrip
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


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        timeout_s: float = 60.0,
        max_retries: int = 2,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._transport = transport

    # ------------------------------------------------------------------ wire
    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _messages(self, req: LLMRequest, spec: ModelSpec) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        send_reasoning = spec.reasoning_roundtrip in (Roundtrip.SIGNATURE, Roundtrip.OPAQUE)

        for msg in req.messages:
            if msg.role is Role.TOOL:
                part = msg.parts[0]
                assert isinstance(part, ToolResultPart)
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": part.call_id,
                        "content": part.content,
                    }
                )
                continue

            entry: dict[str, Any] = {"role": msg.role.value}
            text = msg.text()
            calls = msg.tool_calls()
            reasoning = msg.reasoning()

            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ]
            if reasoning is not None and send_reasoning and not reasoning.redacted:
                # OPAQUE: encrypted token. SIGNATURE via compatible gateways.
                entry["reasoning_content"] = reasoning.opaque or reasoning.text
            if text:
                entry["content"] = text
            elif calls:
                entry["content"] = None
            out.append(entry)
        return out

    def _payload(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": spec.id.split("/", 1)[-1],
            "messages": self._messages(req, spec),
            compiled.max_tokens_param: compiled.max_output_tokens,
        }
        if compiled.temperature is not None:
            payload["temperature"] = compiled.temperature
        if compiled.stop:
            payload["stop"] = list(compiled.stop)

        if compiled.reasoning_style is ReasoningStyle.EFFORT and compiled.reasoning_effort:
            payload["reasoning_effort"] = compiled.reasoning_effort

        if compiled.tools_enabled:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in req.tools
            ]

        if compiled.structured == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": req.response_schema,
                    "strict": False,
                },
            }
        elif compiled.structured == "json_mode":
            payload["response_format"] = {"type": "json_object"}

        return payload

    # ------------------------------------------------------------------ call
    def complete(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> LLMResponse:
        payload = self._payload(req, spec, compiled)
        url = f"{self.base_url}/chat/completions"

        last_error: Optional[ProviderError] = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    transport=self._transport,
                    timeout=min(self.timeout_s, req.timeout_s),
                ) as client:
                    resp = client.post(url, json=payload, headers=self._headers())
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = ProviderError(
                        "provider_unavailable",
                        f"{resp.status_code} from {spec.id}",
                        retryable=True,
                        status=resp.status_code,
                        provider=self.name,
                    )
                    if attempt < self.max_retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise last_error
                if resp.status_code >= 400:
                    raise ProviderError(
                        "provider_rejected",
                        f"{resp.status_code}: {resp.text[:300]}",
                        retryable=False,
                        status=resp.status_code,
                        provider=self.name,
                    )
                return self._parse(resp.json(), spec)
            except httpx.HTTPError as exc:
                last_error = ProviderError(
                    "network", str(exc), retryable=True, provider=self.name
                )
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise last_error

        raise last_error or ProviderError("unknown", "unreachable", provider=self.name)

    # ------------------------------------------------------------------ parse
    def _parse(self, body: dict[str, Any], spec: ModelSpec) -> LLMResponse:
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError("bad_response", f"malformed response: {exc}", provider=self.name)

        reasoning: Optional[ReasoningPart] = None
        raw_reasoning = message.get("reasoning_content") or message.get("reasoning")
        if raw_reasoning:
            reasoning = ReasoningPart(text=str(raw_reasoning))

        calls = tuple(
            ToolCallPart(
                id=c.get("id", f"call_{i}"),
                name=c["function"]["name"],
                arguments=_json_or_empty(c["function"].get("arguments", "{}")),
            )
            for i, c in enumerate(message.get("tool_calls") or [])
        )

        parts: list = []
        if reasoning is not None:
            parts.append(reasoning)
        if message.get("content"):
            parts.append(TextPart(message["content"]))
        parts.extend(calls)

        usage = body.get("usage", {}) or {}
        details = (usage.get("completion_tokens_details") or {}) or {}
        finish = choice.get("finish_reason") or "stop"
        if calls and finish == "stop":
            finish = "tool_calls"

        return LLMResponse(
            message=Message(Role.ASSISTANT, tuple(parts)),
            model=body.get("model", spec.id),
            provider=self.name,
            finish_reason=finish,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                reasoning_tokens=details.get("reasoning_tokens", 0) or 0,
                cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
            ),
            request_id=body.get("id", ""),
        )


def _json_or_empty(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
