"""The agent loop: Mission -> Scene -> Think -> Act -> Observe -> (iterate).

This implements the five-step agentic process from *Introduction to Agents*
(Nov 2025) with three additions the domain demands:

- **Policy before action.** Nothing executes without a scope check (§"Policies").
- **Guardrails before output.** User text passes the deterministic safety router;
  a red band stops the loop and escalates — the model never gets to "handle" it.
- **Bounded autonomy.** Max steps, max tool calls, max wall time. An agent that
  can loop forever is a cost incident waiting to happen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Optional

from ..safety import SafetyBand, classify
from .contracts import AgentResult, Mission, Observation, Scene, Step
from .model import Model, ScriptedPlanner
from .policy import READ, SAFETY, WRITE, Principal, visible_tools
from .registry import Registry, Runtime, ToolContext, ToolError
from .trace import Trace, digest

MAX_STEPS = 8
MAX_TOOL_CALLS = 12
MAX_WALL_SECONDS = 30.0


@dataclass
class Agent:
    registry: Registry
    runtime: Runtime
    model: Optional[Model] = None
    persona: str = (
        "You are Rhythm, a daily energy and recovery coach. One action, never a menu. "
        "Never punish a miss. Only say what a tool or a guideline supports."
    )
    max_steps: int = MAX_STEPS
    max_tool_calls: int = MAX_TOOL_CALLS
    max_seconds: float = MAX_WALL_SECONDS
    audience: str = "rhythm-mcp"

    def run(self, mission: Mission, *, at: Optional[datetime] = None) -> AgentResult:
        trace = Trace("agent.run")
        model = self.model or ScriptedPlanner()
        steps: list[Step] = []
        observations: list[Observation] = []
        started = time.time()

        # -- Step 1: Get the Mission ----------------------------------------
        steps.append(Step(0, "mission", mission.goal))
        trace.event("mission", goal=mission.goal, user_id=mission.user_id)

        # -- Guardrail: mission text is user input, before anything else -----
        verdict = classify(mission.raw_text or mission.goal)
        if verdict.band is SafetyBand.RED:
            steps.append(Step(1, "stop", "red flag: fixed protocol, model never speaks"))
            trace.finish("error")
            return AgentResult(
                answer=verdict.message or "This needs a clinician, not a coaching app.",
                steps=steps,
                observations=observations,
                stopped_reason="safety_red",
                trace_id=trace.trace_id,
            )

        ctx = ToolContext(
            principal=Principal(
                agent_id="rhythm-coach",
                subject=mission.user_id,
                scopes=frozenset({READ, SAFETY, WRITE}),
            ),
            runtime=self.runtime,
            trace=trace,
        )
        at = at or datetime.combine(mission.day, dtime(9, 0))

        try:
            for step_index in range(1, self.max_steps + 1):
                if time.time() - started > self.max_seconds:
                    return self._stop(steps, observations, "budget_time", trace)
                if len(observations) >= self.max_tool_calls:
                    return self._stop(steps, observations, "budget_tools", trace)

                # -- Step 2: Scan the Scene --------------------------------
                scene = Scene(
                    user_id=mission.user_id,
                    day=mission.day,
                    at=at,
                    facts={"goal": mission.goal, "raw_text": mission.raw_text},
                    memory=self._memory_slice(mission),
                    available_tools=visible_tools(self.registry.specs(), ctx.principal),
                    persona=self.persona,
                )
                steps.append(Step(step_index, "scan", f"{len(scene.available_tools)} tools visible"))

                # -- Step 3: Think It Through ------------------------------
                decision = model.decide(scene, observations)
                if decision.thought_private:
                    # Model CoT never leaves the process in clear text: the step
                    # and the trace carry a digest; round-trip uses the live copy.
                    detail = f"reasoning digest {digest(decision.thought)} ({len(decision.thought)} chars)"
                    trace.event("decision", thought_digest=digest(decision.thought), done=decision.done)
                else:
                    detail = decision.thought
                    trace.event("decision", thought=decision.thought, done=decision.done)
                steps.append(Step(step_index, "think", detail))

                if decision.done:
                    steps.append(Step(step_index, "stop", "complete"))
                    trace.finish("ok")
                    return AgentResult(
                        answer=decision.answer or self._fallback_answer(observations),
                        steps=steps,
                        observations=observations,
                        stopped_reason="complete",
                        trace_id=trace.trace_id,
                    )

                # -- Step 4: Take Action (registry enforces policy + schema)
                for call in decision.tool_calls:
                    if len(observations) >= self.max_tool_calls:
                        break
                    t0 = time.time()
                    try:
                        result = self.registry.call(
                            call.name, dict(call.arguments), ctx, audience=self.audience
                        )
                        observations.append(
                            Observation(
                                call=call,
                                ok=True,
                                result=result,
                                latency_ms=round((time.time() - t0) * 1000, 2),
                            )
                        )
                        steps.append(Step(step_index, "act", call.name))
                    except ToolError as exc:
                        observations.append(
                            Observation(
                                call=call,
                                ok=False,
                                error=exc.message,
                                error_code=exc.code,
                                latency_ms=round((time.time() - t0) * 1000, 2),
                            )
                        )
                        steps.append(Step(step_index, "observe", f"{call.name} failed: {exc.code}"))
                        trace.event("tool_error", tool=call.name, code=exc.code)
                # -- Step 5: Observe and Iterate — the loop continues -------

            return self._stop(steps, observations, "max_steps", trace)

        except Exception as exc:  # noqa: BLE001 — recorded, then converted honestly
            trace.finish("error")
            steps.append(Step(len(steps), "stop", f"unexpected: {type(exc).__name__}"))
            return AgentResult(
                answer="Something went wrong on my side. Nothing was changed.",
                steps=steps,
                observations=observations,
                stopped_reason="error",
                trace_id=trace.trace_id,
            )

    # ---------------------------------------------------------------- helpers
    def _memory_slice(self, mission: Mission) -> dict:
        """Context engineering: the last seven days of attempts, nothing more."""
        from datetime import timedelta

        store = self.runtime.loop.store
        items = store.commitments_between(mission.user_id, mission.day - timedelta(days=6), mission.day)
        return {
            "attempts": [
                {"day": c.day.isoformat(), "status": c.status, "action": c.action_id}
                for c in items
            ][-7:]
        }

    @staticmethod
    def _fallback_answer(observations: list[Observation]) -> str:
        if not observations:
            return "I could not gather anything useful. Please try again."
        last = observations[-1]
        if last.ok and isinstance(last.result, dict):
            for key in ("proposal", "review", "message", "sleep_h"):
                if key in last.result and last.result[key] is not None:
                    return str(last.result[key])
        return "Done."

    @staticmethod
    def _stop(
        steps: list[Step], observations: list[Observation], reason: str, trace: Trace
    ) -> AgentResult:
        steps.append(Step(len(steps), "stop", reason))
        trace.finish("error")
        return AgentResult(
            answer=f"Stopped: {reason}.",
            steps=steps,
            observations=observations,
            stopped_reason=reason,
            trace_id=trace.trace_id,
        )
