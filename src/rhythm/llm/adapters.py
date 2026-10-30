"""Bridge: any routed model → the existing agent `Model` protocol.

The agent loop (rhythm/agent/agent.py) does not change to adopt a new model —
that is the point of this adapter. It also enforces the reasoning-model rules
the product cares about:

1. **Reasoning is never an answer.** Thinking text goes to the trace only; it
   never reaches the user and never substitutes for a missing tool result.
2. **Native tool calls are the happy path.** Reasoning models are trained to
   *call tools*, not to emit JSON-in-text. The planner maps neutral
   `ToolCallPart`s straight onto the loop. A JSON decision payload is still
   accepted for models without tool support.
3. **Degradation, not failure.** If routing fails — or the model skips its tools
   and returns unparseable text before doing any work — the scripted planner
   takes the step. A coaching miss costs less than a crash.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..agent.contracts import Decision, Observation, Scene, ToolCall
from ..agent.model import Model, ScriptedPlanner
from ..agent.registry import Registry
from ..jsonschema_lite import validate
from .messages import LLMRequest, Message, ToolSchema
from .router import Router
from .structured import _extract_json

# Decision contract for models WITHOUT native tool support. Models that do
# support tools should call them natively; this schema is the fallback shape.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["thought", "done"],
    "additionalProperties": False,
    "properties": {
        "thought": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "arguments"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
            },
        },
        "done": {"type": "boolean"},
        "answer": {"type": "string"},
    },
}


class LLMPlanner(Model):
    name = "llm-planner"

    def __init__(
        self,
        router: Router,
        registry: Registry,
        *,
        task: str = "coach",
        fallback: Optional[Model] = None,
        reasoning_budget_tokens: Optional[int] = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.task = task
        self.fallback = fallback or ScriptedPlanner()
        self.reasoning_budget_tokens = reasoning_budget_tokens
        self.last_reasoning: str = ""
        self.last_degraded: bool = False

    # ------------------------------------------------------------------ api
    def decide(self, scene: Scene, transcript: list[Observation]) -> Decision:
        messages = self._messages(scene, transcript)
        tools = self._tools(scene)

        try:
            response = self.router.complete(
                self.task,
                LLMRequest(
                    messages=tuple(messages),
                    tools=tools,
                    max_output_tokens=800,
                    reasoning_budget_tokens=self.reasoning_budget_tokens,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the coach
            return self._degraded(scene, transcript, f"routing failed: {type(exc).__name__}")

        reasoning = response.reasoning
        self.last_reasoning = reasoning.text if reasoning is not None else ""
        self.last_degraded = False

        # -- 1. native tool calls: the reasoning-model happy path ------------
        if response.tool_calls:
            calls = self._map_calls(response.tool_calls, scene)
            thought = self.last_reasoning or response.text
            return Decision(
                thought=thought, tool_calls=calls, done=False, thought_private=True
            )

        # -- 2. JSON decision payload (models without tool support) ----------
        value = _extract_json(response.text)
        if value is not None and not validate(DECISION_SCHEMA, value):
            calls = self._map_calls(
                [
                    type("C", (), {"name": c["name"], "arguments": c.get("arguments") or {}})
                    for c in value.get("tool_calls", [])
                    if isinstance(c, dict) and c.get("name")
                ],
                scene,
            )
            return Decision(
                thought=value.get("thought", ""),
                tool_calls=calls,
                done=bool(value.get("done")) and not calls,
                answer=value.get("answer", ""),
                thought_private=True,
            )

        # -- 3. plain text ----------------------------------------------------
        # After real work, plain text is the final answer. Before any work, an
        # unparseable reply means the model skipped its tools: degrade.
        if transcript and any(o.ok for o in transcript):
            return Decision(
                thought=self.last_reasoning,
                tool_calls=(),
                done=True,
                answer=response.text,
                thought_private=True,
            )
        return self._degraded(scene, transcript, "model skipped its tools")

    # -------------------------------------------------------------- internals
    def _map_calls(self, calls: Any, scene: Scene) -> tuple[ToolCall, ...]:
        """Model names use underscores; registry names use dots. Filter to what
        this principal may actually call — the model never expands its powers."""
        allowed = set(scene.available_tools)
        out: list[ToolCall] = []
        for c in calls:
            name = _registry_name(c.name)
            if name in allowed:
                out.append(ToolCall(name=name, arguments=c.arguments or {}))
        return tuple(out)

    def _degraded(self, scene: Scene, transcript: list[Observation], why: str) -> Decision:
        self.last_degraded = True
        self.last_reasoning = ""
        decision = self.fallback.decide(scene, transcript)
        return Decision(
            thought=f"[degraded to {self.fallback.name}: {why}] " + decision.thought,
            tool_calls=decision.tool_calls,
            done=decision.done,
            answer=decision.answer,
            thought_private=decision.thought_private,
        )

    def _tools(self, scene: Scene) -> tuple[ToolSchema, ...]:
        """Only tools the principal may call, with full schemas for the model."""
        allowed = set(scene.available_tools)
        out = []
        for spec in self.registry.specs():
            if spec.name in allowed:
                out.append(
                    ToolSchema(
                        name=_mcp_name(spec.name),
                        description=f"{spec.description} [id: {spec.name}]",
                        input_schema=spec.input_schema,
                    )
                )
        return tuple(out)

    def _messages(self, scene: Scene, transcript: list[Observation]) -> list[Message]:
        messages = [Message.system(scene.persona)]

        history = "\n".join(
            f"- {o.call.name}({o.call.arguments}) -> "
            + (f"ok: {json_brief(o.result)}" if o.ok else f"error {o.error_code}: {o.error}")
            for o in transcript
        )
        user_block = (
            f"Goal: {scene.facts.get('goal', '')}\n"
            f"Day: {scene.day.isoformat()}\n"
            f"Known state: {json_brief(scene.facts)}\n"
            f"Memory (last attempts): {json_brief(scene.memory)}\n"
            f"Previous tool results:\n{history or '(none)'}\n\n"
            "Call the tools you need. When the goal is met, reply with your final "
            "answer as plain text (no JSON, no tool call) — 60 words max, in the "
            "person's language. Never invent data; never show percentages a tool "
            "did not return."
        )
        messages.append(Message.user(user_block))
        return messages


def _mcp_name(registry_name: str) -> str:
    """rhythm.day.state -> rhythm_day_state. Unambiguous: registry names never
    contain underscores, so the mapping round-trips."""
    return registry_name.replace(".", "_")


def _registry_name(mcp_name: str) -> str:
    return mcp_name.replace("_", ".")


def json_brief(value: Any, limit: int = 400) -> str:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
