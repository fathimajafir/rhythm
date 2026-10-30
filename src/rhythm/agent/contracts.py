"""Explicit data contracts for the agent loop.

Naming follows the five-step agentic process in *Introduction to Agents* (Nov 2025):
Mission → Scene → Think → Act → Observe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


@dataclass(frozen=True)
class Mission:
    """Step 1 — Get the Mission. A specific, high-level goal from a user or trigger."""

    goal: str
    user_id: str
    day: date
    raw_text: str = ""
    constraints: tuple[str, ...] = ()
    deadline: Optional[datetime] = None


@dataclass(frozen=True)
class Scene:
    """Step 2 — Scan the Scene. What the agent can see *right now*.

    Context engineering lives here: only the slices relevant to this step, never
    the whole store. `available_tools` is already filtered by policy.
    """

    user_id: str
    day: date
    at: datetime
    facts: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    available_tools: tuple[str, ...] = ()
    persona: str = ""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class Decision:
    """Step 3 — Think It Through. The model's output: reasoning + next actions."""

    thought: str
    tool_calls: tuple[ToolCall, ...] = ()
    done: bool = False
    answer: str = ""
    # True when `thought` may contain model chain-of-thought. The loop then keeps
    # only a digest in steps/traces: CoT must not leave the process in clear text.
    thought_private: bool = False


@dataclass(frozen=True)
class Observation:
    """Step 5 — Observe. The result of an action, added to context."""

    call: ToolCall
    ok: bool
    result: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    latency_ms: float = 0.0


@dataclass(frozen=True)
class Step:
    index: int
    kind: str  # mission | scan | think | act | observe | reflect | stop
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    answer: str
    steps: list[Step]
    observations: list[Observation]
    stopped_reason: str
    trace_id: str
    tokens: int = 0
    tool_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.stopped_reason in ("complete",)
