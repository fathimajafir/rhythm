"""The model boundary.

*Introduction to Agents* separates the Model (brain) from Tools (hands) from the
Orchestration layer. This module is the only place a model enters, which is what
lets Phase 1 run with none and Phase 2 swap one in without touching the loop.

`ScriptedPlanner` is a deterministic rule planner. It is deliberately not called
an LLM, and it is used in tests and as the fallback when the model layer is down
or over budget.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence

from .contracts import Decision, Observation, Scene, ToolCall


class Model(Protocol):
    """Anything that can turn a scene into a decision."""

    name: str

    def decide(self, scene: Scene, transcript: Sequence[Observation]) -> Decision: ...


@dataclass
class CallbackModel:
    """Wrap a real model with the smallest possible seam.

    `complete(prompt) -> str` returns JSON: {"thought": ..., "tool_calls": [...],
    "done": bool, "answer": ...}. The loop does not care which vendor produced it.
    """

    complete: Callable[[str], str]
    name: str = "callback-model"

    def decide(self, scene: Scene, transcript: Sequence[Observation]) -> Decision:
        prompt = build_prompt(scene, transcript)
        raw = self.complete(prompt)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:  # fail closed, do not guess
            raise ValueError(f"model returned non-JSON: {raw[:200]}") from exc
        return Decision(
            thought=payload.get("thought", ""),
            tool_calls=tuple(
                ToolCall(name=c["name"], arguments=c.get("arguments", {}))
                for c in payload.get("tool_calls", [])
            ),
            done=bool(payload.get("done", False)),
            answer=payload.get("answer", ""),
        )


def build_prompt(scene: Scene, transcript: Sequence[Observation]) -> str:
    """Context engineering: only the current scene and the calls already made."""
    tools = "\n".join(f"- {t}" for t in scene.available_tools)
    history = "\n".join(
        f"- {o.call.name}({o.call.arguments}) -> {'ok' if o.ok else o.error_code}"
        for o in transcript
    )
    return (
        f"{scene.persona}\n\n"
        f"Today: {scene.day.isoformat()}. Facts: {json.dumps(scene.facts, default=str)}\n"
        f"Available tools:\n{tools}\n\n"
        f"Calls so far:\n{history or '(none)'}\n\n"
        "Respond with JSON only: {thought, tool_calls: [{name, arguments}], done, answer}."
    )


class ScriptedPlanner:
    """Deterministic planner used by tests, demos and as the offline fallback."""

    name = "scripted-planner-v0"

    # intent -> ordered tool plan. The loop still acts/observes one call at a time.
    PLANS: dict[str, tuple[tuple[str, dict], ...]] = {
        "checkin": (
            ("rhythm.checkin", {"energy": 3}),
            ("rhythm.plan.preview", {}),
        ),
        "plan": (("rhythm.plan.preview", {}),),
        "summary": (("rhythm.sleep.summary", {}),),
        "review": (("rhythm.review.weekly", {}),),
        "safety": (("rhythm.safety.classify", {}),),
        "library": (("rhythm.action.library", {"limit": 5}),),
    }

    def __init__(self, intent: Optional[str] = None) -> None:
        self.intent = intent

    def classify(self, goal: str) -> str:
        if self.intent:
            return self.intent
        g = goal.lower()
        if any(w in g for w in ("check in", "check-in", "how did i sleep", "morning")):
            return "checkin"
        if any(w in g for w in ("plan", "what should i do", "today's action")):
            return "plan"
        if any(w in g for w in ("week", "review", "how am i doing")):
            return "review"
        if any(w in g for w in ("chest", "pain", "hurt", "sick", "symptom")):
            return "safety"
        if any(w in g for w in ("sleep", "slept", "tired", "debt")):
            return "summary"
        return "library"

    def decide(self, scene: Scene, transcript: Sequence[Observation]) -> Decision:
        plan = self.PLANS[self.classify(scene.facts.get("goal", ""))]
        step = len(transcript)

        if step >= len(plan):
            return Decision(
                thought="plan exhausted; synthesise from observations",
                done=True,
                answer=self._answer(transcript),
            )

        name, base_args = plan[step]
        args = {
            "user_id": scene.user_id,
            "day": scene.day.isoformat(),
            **base_args,
        }
        if name == "rhythm.checkin":
            args["at"] = scene.at.isoformat()
        if name == "rhythm.safety.classify":
            args = {"text": scene.facts.get("raw_text", "")}
        return Decision(
            thought=f"step {step + 1}/{len(plan)}: {name}",
            tool_calls=(ToolCall(name=name, arguments=args),),
        )

    def _answer(self, transcript: Sequence[Observation]) -> str:
        if not transcript:
            return "I have nothing to report yet."
        last = transcript[-1]
        if not last.ok:
            return f"I couldn't complete that: {last.error}"
        data = last.result if isinstance(last.result, dict) else {}
        if data.get("proposal"):
            return str(data["proposal"])
        if data.get("review"):
            return str(data["review"])
        if "primary" in data and data.get("primary") is None:
            return f"No plan yet — {data.get('reason', 'check in first')}"
        if "primary" in data and isinstance(data["primary"], dict):
            return f"Today: {data['primary'].get('title_en', 'an action')}."
        if "actions" in data:
            return f"There are {data.get('total', len(data['actions']))} actions available."
        if "band" in data:
            return str(data.get("message") or "No safety concern detected.")
        if "sleep_h" in data:
            return f"Last night: {data['sleep_h']}h sleep, debt {data.get('sleep_debt_h')}h."
        return "Done."
