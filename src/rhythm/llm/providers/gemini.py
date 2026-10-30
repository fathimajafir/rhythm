"""
Google Gemini adapter (generateContent), with thinkingConfig.

Gemini expresses reasoning as a token budget (`thinkingConfig.thinkingBudget`),
optionally surfacing thought summaries as parts with `thought: true`. Function
calling rides in `functionDeclarations`, and results come back as
`functionResponse` parts — a different shape from OpenAI's `role: "tool"`, which
is exactly why the neutral message model exists.
"""
from __future__ import annotations

import json
import time

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

FINISH_MAP = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter"}


class GeminiProvider:
    name = "google"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 90.0,
        max_retries: int = 2,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json", "x-goog-api-key": self.api_key}

    def _contents(self, req: LLMRequest, spec: ModelSpec) -> list[dict[str, Any]]:
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
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": part.call_id,  # adapter stores name in call_id
                                    "response": {"result": part.content},
                                }
                            }
                        ],
                    }
                )
                continue

            parts: list[dict[str, Any]] = []
            for p in msg.parts:
                if isinstance(p, ReasoningPart) and p.text and not p.redacted:
                    # Thought summaries are display-only; do not echo as content.
                    continue
                if isinstance(p, ToolCallPart):
                    parts.append({"functionCall": {"name": p.name, "args": p.arguments}})
                elif isinstance(p, TextPart) and p.text:
                    parts.append({"text": p.text})
            if not parts:
                parts = [{"text": ""}]
            out.append(
                {"role": "model" if msg.role is Role.ASSISTANT else "user", "parts": parts}
            )
        return out

    def _payload(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> dict[str, Any]:
        payload: dict[str, Any] = {"contents": self._contents(req, spec)}

        system_blocks = [
            p.text
            for m in req.messages
            if m.role is Role.SYSTEM
            for p in m.parts
            if isinstance(p, TextPart)
        ]
        if system_blocks:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_blocks)}]}

        gen: dict[str, Any] = {"maxOutputTokens": compiled.max_output_tokens}
        if compiled.temperature is not None:
            gen["temperature"] = compiled.temperature
        if compiled.stop:
            gen["stopSequences"] = list(compiled.stop)

        if compiled.reasoning_style is ReasoningStyle.BUDGET:
            gen["thinkingConfig"] = {
                "thinkingBudget": compiled.reasoning_budget or 0,
                "includeThoughts": bool(compiled.reasoning_budget),
            }
        payload["generationConfig"] = gen

        if compiled.tools_enabled:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.input_schema,
                        }
                        for t in req.tools
                    ]
                }
            ]
        return payload

    def complete(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> LLMResponse:
        model = spec.id.split("/", 1)[-1]
        url = f"{self.base_url}/models/{model}:generateContent"
        payload = self._payload(req, spec, compiled)

        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    transport=self._transport,
                    timeout=min(self.timeout_s, req.timeout_s),
                ) as client:
                    resp = client.post(url, json=payload, headers=self._headers())
                if resp.status_code in (429, 500, 502, 503):
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

    def _parse(self, body: dict[str, Any], spec: ModelSpec) -> LLMResponse:
        model = spec.id.split("/", 1)[-1]
        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderError(
                "bad_response",
                f"no candidates: {json.dumps(body.get('promptFeedback', {}))[:200]}",
                provider=self.name,
            )
        content = candidates[0].get("content") or {}
        parts: list = []
        for raw in content.get("parts", []):
            if raw.get("thought"):
                parts.append(ReasoningPart(text=raw.get("text", ""), redacted=True))
            elif "functionCall" in raw:
                fc = raw["functionCall"]
                parts.append(
                    ToolCallPart(
                        id=fc["name"],  # Gemini has no call id; name is the binding key
                        name=fc["name"],
                        arguments=fc.get("args") or {},
                    )
                )
            elif raw.get("text"):
                parts.append(TextPart(raw["text"]))

        usage = body.get("usageMetadata", {}) or {}
        return LLMResponse(
            message=Message(Role.ASSISTANT, tuple(parts)),
            model=model,
            provider=self.name,
            finish_reason=FINISH_MAP.get(candidates[0].get("finishReason", "STOP"), "stop"),
            usage=Usage(
                input_tokens=usage.get("promptTokenCount", 0),
                output_tokens=usage.get("candidatesTokenCount", 0),
                reasoning_tokens=usage.get("thoughtsTokenCount", 0),
            ),
        )

