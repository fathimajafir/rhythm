"""Provider-neutral message model.

The important design point: **reasoning is a first-class part, not text**.

Reasoning models differ in what they emit and what they will accept back:
- OpenAI reasoning models return summary/reasoning items that must be replayed
  for stateless tool loops (encrypted) or stored server-side;
- Anthropic returns `thinking` blocks with a `signature` that must be returned
  verbatim on the next turn when tools are in play;
- DeepSeek/Qwen-style OpenAI-compatible servers expose `reasoning_content` and
  explicitly do **not** want it echoed back.

A single `text` field cannot represent any of that, so `ReasoningPart` carries
the text plus whatever round-trip token the provider needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ReasoningPart:
    """Chain-of-thought. Never shown to the user; carried only for round-trip."""

    text: str = ""
    signature: Optional[str] = None  # Anthropic thinking signature
    opaque: Optional[str] = None  # OpenAI encrypted reasoning item
    redacted: bool = False  # provider returned a summary/redaction, not raw CoT


@dataclass(frozen=True)
class ToolCallPart:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResultPart:
    call_id: str
    content: str
    is_error: bool = False


Part = Union[TextPart, ReasoningPart, ToolCallPart, ToolResultPart]


@dataclass(frozen=True)
class Message:
    role: Role
    parts: tuple[Part, ...] = ()

    # -- convenience constructors -----------------------------------------
    @staticmethod
    def system(text: str) -> "Message":
        return Message(Role.SYSTEM, (TextPart(text),))

    @staticmethod
    def user(text: str) -> "Message":
        return Message(Role.USER, (TextPart(text),))

    @staticmethod
    def assistant(text: str = "", *, reasoning: Optional[ReasoningPart] = None) -> "Message":
        parts: list[Part] = []
        if reasoning is not None:
            parts.append(reasoning)
        if text:
            parts.append(TextPart(text))
        return Message(Role.ASSISTANT, tuple(parts))

    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    def reasoning(self) -> Optional[ReasoningPart]:
        return next((p for p in self.parts if isinstance(p, ReasoningPart)), None)

    def tool_calls(self) -> tuple[ToolCallPart, ...]:
        return tuple(p for p in self.parts if isinstance(p, ToolCallPart))


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...] = ()
    response_schema: Optional[dict[str, Any]] = None
    max_output_tokens: int = 800
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None  # minimal | low | medium | high
    reasoning_budget_tokens: Optional[int] = None
    stop: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 60.0


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
        )


@dataclass(frozen=True)
class LLMResponse:
    message: Message
    model: str
    provider: str
    finish_reason: str = "stop"  # stop | length | tool_calls | content_filter | error
    usage: Usage = field(default_factory=Usage)
    request_id: str = ""
    notes: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return self.message.text()

    @property
    def tool_calls(self) -> tuple[ToolCallPart, ...]:
        return self.message.tool_calls()

    @property
    def reasoning(self) -> Optional[ReasoningPart]:
        return self.message.reasoning()
