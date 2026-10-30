"""The model portability layer.

Public surface:
- `messages`   — provider-neutral content model (reasoning is a first-class part)
- `capabilities` — per-model capability specs; branch on capability, never on name
- `compiler`   — capability-correct request compilation, with an audit trail
- `providers`  — wire adapters (OpenAI-compatible, Anthropic, Gemini)
- `router`     — task routes, fallback chains, spend budget, reasoning escalation
- `structured` — schema-validated output with a repair loop
- `adapters`   — `LLMPlanner`: plugs any routed model into the agent loop

Rules this layer enforces project-wide:
1. The agent never imports a vendor SDK.
2. All numbers stay in Python (§ design doc) — the LLM layer only talks.
3. Reasoning text is trace-only. It is never shown to a user and never counted
   as an answer.
4. Unknown model → conservative, unverified capabilities. Fail closed, not slow.
"""
from .adapters import LLMPlanner
from .capabilities import ModelSpec, ReasoningStyle, Roundtrip, all_specs, resolve
from .compiler import Compiled, compile_request
from .messages import (
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSchema,
    Usage,
)
from .router import Budget, BudgetExceeded, Router, RoutingExhausted, TaskRoute
from .structured import StructuredOutputError, request_structured

__all__ = [
    "Budget",
    "BudgetExceeded",
    "Compiled",
    "Compiled",
    "LLMPlanner",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ModelSpec",
    "ReasoningPart",
    "ReasoningStyle",
    "Role",
    "Roundtrip",
    "Router",
    "RoutingExhausted",
    "StructuredOutputError",
    "TaskRoute",
    "TextPart",
    "ToolCallPart",
    "ToolResultPart",
    "ToolSchema",
    "Usage",
    "all_specs",
    "compile_request",
    "request_structured",
    "resolve",
]
