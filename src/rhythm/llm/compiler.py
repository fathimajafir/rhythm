"""Capability-aware request compilation.

Every provider/model quirk is resolved here, once, so provider adapters stay dumb
and the agent never learns a vendor's rules.

The compiler is **auditable**: it returns notes for every decision it made, and
those notes go into the trace. "Why did we drop temperature?" has an answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .capabilities import ModelSpec, ReasoningStyle
from .messages import LLMRequest

EFFORT_ORDER = ("minimal", "low", "medium", "high")

# Sensible per-task reasoning budgets (tokens). Small on purpose: the daily loop
# must stay bounded, and a coach doesn't need a research-grade think.
DEFAULT_BUDGET_BY_TASK: dict[str, int] = {
    "triage": 0,
    "coach": 1_024,
    "review": 2_048,
    "safety_review": 1_024,
    "deep_plan": 4_096,
}


@dataclass(frozen=True)
class Compiled:
    temperature: Optional[float]
    max_output_tokens: int
    max_tokens_param: str
    reasoning_style: ReasoningStyle
    reasoning_effort: Optional[str]
    reasoning_budget: Optional[int]
    tools_enabled: bool
    parallel_tools: bool
    structured: str  # json_schema | json_mode | prompt_only
    stop: tuple[str, ...]
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "reasoning": {
                "style": self.reasoning_style.value,
                "effort": self.reasoning_effort,
                "budget": self.reasoning_budget,
            },
            "tools": self.tools_enabled,
            "structured": self.structured,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }


def compile_request(req: LLMRequest, spec: ModelSpec, *, task: str = "coach") -> Compiled:
    notes: list[str] = []
    warnings: list[str] = []

    # -- output budget ------------------------------------------------------
    max_out = req.max_output_tokens
    if max_out > spec.max_output:
        notes.append(f"max_output {max_out} clamped to model limit {spec.max_output}")
        max_out = spec.max_output

    # -- reasoning ----------------------------------------------------------
    effort: Optional[str] = None
    budget: Optional[int] = None
    style = spec.reasoning

    if style is ReasoningStyle.EFFORT:
        effort = req.reasoning_effort or "medium"
        allowed = _efforts_up_to(spec.max_reasoning_effort)
        if effort not in allowed:
            notes.append(f"reasoning_effort '{effort}' not supported; using '{allowed[-1]}'")
            effort = allowed[-1]
    elif style is ReasoningStyle.BUDGET:
        desired = req.reasoning_budget_tokens
        if desired is None:
            desired = DEFAULT_BUDGET_BY_TASK.get(task, 1_024)
        # Thinking budget must leave room to answer.
        budget = max(0, min(desired, max(256, max_out // 2)))
        if budget == 0:
            notes.append("reasoning disabled for this task")
        # Always keep headroom for the actual answer.
        if max_out <= budget:
            max_out = min(spec.max_output, budget + 512)
            notes.append(f"max_output raised to {max_out} to leave room after thinking")
    elif style is ReasoningStyle.TOGGLE:
        enabled = (req.reasoning_budget_tokens or DEFAULT_BUDGET_BY_TASK.get(task, 0)) > 0
        effort = "on" if enabled else "off"
    else:
        if req.reasoning_effort or req.reasoning_budget_tokens:
            warnings.append(
                "reasoning requested but model has no reasoning mode; request ignored"
            )

    # -- sampling params ----------------------------------------------------
    temperature = req.temperature
    reasoning_active = style is not ReasoningStyle.NONE and (
        effort not in (None, "off") or bool(budget)
    )
    if not spec.supports_temperature:
        if temperature is not None:
            notes.append("temperature omitted: model does not accept it")
        temperature = None
    elif reasoning_active and style is ReasoningStyle.BUDGET:
        # Anthropic-style: thinking requires the default temperature.
        if temperature is not None:
            notes.append("temperature omitted: required by thinking mode")
        temperature = None

    stop = req.stop if spec.supports_stop else ()
    if req.stop and not spec.supports_stop:
        notes.append("stop sequences omitted: model does not accept them")

    # -- tools --------------------------------------------------------------
    tools_enabled = bool(req.tools) and spec.supports_tools
    if req.tools and not spec.supports_tools:
        warnings.append(
            f"{spec.id} is not registered as tool-capable; tools disabled "
            "(register a ModelSpec to enable)"
        )

    # -- structured output --------------------------------------------------
    if req.response_schema is None:
        structured = "none"
    elif spec.supports_json_schema:
        structured = "json_schema"
    elif spec.supports_json_mode:
        structured = "json_mode"
        notes.append("json_schema unsupported; using json_mode + validation")
    else:
        structured = "prompt_only"
        warnings.append("no native structured output; schema injected into the prompt")

    if not spec.verified:
        warnings.append(
            f"model '{spec.id}' is not in the capability registry — running with "
            "conservative defaults. Register it to unlock tools/structured output."
        )

    return Compiled(
        temperature=temperature,
        max_output_tokens=max_out,
        max_tokens_param=spec.max_tokens_param,
        reasoning_style=style,
        reasoning_effort=effort,
        reasoning_budget=budget,
        tools_enabled=tools_enabled,
        parallel_tools=spec.parallel_tool_calls and tools_enabled,
        structured=structured,
        stop=stop,
        notes=tuple(notes),
        warnings=tuple(warnings),
    )


def _efforts_up_to(maximum: str) -> list[str]:
    try:
        idx = EFFORT_ORDER.index(maximum)
    except ValueError:
        idx = len(EFFORT_ORDER) - 1
    return list(EFFORT_ORDER[: idx + 1])
