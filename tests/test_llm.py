"""Model-portability tests.

No network anywhere: OpenAI-compatible / Anthropic / Gemini adapters are exercised
over httpx.MockTransport, and routing/planner behaviour over fake providers.

What these tests pin down (the reasons the layer exists):
- capability-driven compilation (reasoning models lose temperature, gain budgets);
- reasoning round-trip differences per vendor;
- router fallback, budget degradation and reasoning escalation;
- structured output repair;
- the agent loop works unchanged on a routed reasoning model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rhythm.llm.capabilities import (  # noqa: E402
    ModelSpec,
    ReasoningStyle,
    Roundtrip,
    resolve,
)
from rhythm.llm.compiler import compile_request  # noqa: E402
from rhythm.llm.messages import (  # noqa: E402
    LLMRequest,
    Message,
    ReasoningPart,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSchema,
)
from rhythm.llm.providers.anthropic import AnthropicProvider  # noqa: E402
from rhythm.llm.providers.gemini import GeminiProvider  # noqa: E402
from rhythm.llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from rhythm.llm.router import (  # noqa: E402
    Budget,
    BudgetExceeded,
    Router,
    RoutingExhausted,
    TaskRoute,
)
from rhythm.llm.structured import StructuredOutputError, request_structured  # noqa: E402
from rhythm.llm.adapters import LLMPlanner  # noqa: E402
from rhythm.agent import Agent, build_registry  # noqa: E402
from rhythm.agent.contracts import Mission, Observation, Scene, ToolCall  # noqa: E402
from rhythm.agent.registry import Runtime  # noqa: E402
from rhythm.agent.model import ScriptedPlanner  # noqa: E402
from rhythm.llm.messages import LLMResponse, Usage  # noqa: E402
from datetime import date  # noqa: E402

DAY = date(2026, 10, 26)

DECISION = {"thought": "ok", "tool_calls": [], "done": True, "answer": "Done."}


# ---------------------------------------------------------------------------
# capability resolution
# ---------------------------------------------------------------------------
def test_exact_and_family_resolution():
    assert resolve("openai/o4-mini").reasoning is ReasoningStyle.EFFORT
    # a point release inherits the family's capabilities
    spec = resolve("openai/gpt-5-turbo-2026-11")
    assert spec.reasoning is ReasoningStyle.EFFORT
    assert spec.supports_temperature is False
    assert spec.id == "openai/gpt-5-turbo-2026-11"


def test_unknown_model_fails_closed_and_is_flagged():
    spec = resolve("vendor-x/super-model-9")
    assert spec.verified is False
    assert spec.supports_tools is False
    assert spec.supports_temperature is False


def test_overrides_register_new_models(tmp_path, monkeypatch):
    path = tmp_path / "specs.json"
    path.write_text(json.dumps({
        "vendor-x/super-model-9": {
            "provider": "openai_compat",
            "supports_tools": True,
            "supports_temperature": True,
            "verified": True,
        }
    }))
    monkeypatch.setenv("RHYTHM_MODEL_SPECS", str(path))
    spec = resolve("vendor-x/super-model-9")
    assert spec.verified is True
    assert spec.supports_tools is True


# ---------------------------------------------------------------------------
# compilation: the capability rules
# ---------------------------------------------------------------------------
def _spec(**kw) -> ModelSpec:
    base = dict(
        id="test/model", provider="openai_compat", verified=True,
        supports_temperature=True, supports_stop=True, max_output=4096,
    )
    base.update(kw)
    return ModelSpec(**base)  # type: ignore[arg-type]


def test_reasoning_effort_model_drops_sampling_params():
    spec = _spec(
        reasoning=ReasoningStyle.EFFORT, supports_temperature=False, supports_stop=False,
        max_tokens_param="max_completion_tokens",
    )
    req = LLMRequest(
        messages=(Message.user("hi"),),
        temperature=0.3, stop=("END",), reasoning_effort="high", max_output_tokens=2048,
    )
    compiled = compile_request(req, spec)
    assert compiled.temperature is None
    assert compiled.stop == ()
    assert compiled.reasoning_effort == "high"
    assert compiled.max_tokens_param == "max_completion_tokens"


def test_reasoning_effort_clamped_to_model_capability():
    spec = _spec(reasoning=ReasoningStyle.EFFORT, max_reasoning_effort="medium")
    req = LLMRequest(messages=(Message.user("hi"),), reasoning_effort="high")
    compiled = compile_request(req, spec)
    assert compiled.reasoning_effort == "medium"


def test_thinking_budget_keeps_headroom_for_the_answer():
    spec = _spec(reasoning=ReasoningStyle.BUDGET, max_output=2048)
    req = LLMRequest(messages=(Message.user("hi"),), max_output_tokens=1024, reasoning_budget_tokens=1500)
    compiled = compile_request(req, spec)
    assert compiled.reasoning_budget <= compiled.max_output_tokens // 2
    assert compiled.max_output_tokens > compiled.reasoning_budget


def test_thinking_forces_temperature_default():
    spec = _spec(reasoning=ReasoningStyle.BUDGET, supports_temperature=True)
    req = LLMRequest(messages=(Message.user("hi"),), temperature=0.2, reasoning_budget_tokens=512)
    compiled = compile_request(req, spec)
    assert compiled.temperature is None


def test_classic_model_keeps_temperature_and_gets_no_reasoning():
    spec = _spec(reasoning=ReasoningStyle.NONE)
    req = LLMRequest(messages=(Message.user("hi"),), temperature=0.2, reasoning_effort="high")
    compiled = compile_request(req, spec)
    assert compiled.temperature == 0.2
    assert compiled.reasoning_effort is None
    assert any("no reasoning mode" in w for w in compiled.warnings)


def test_unregistered_model_disables_tools_with_warning():
    spec = _spec(verified=False, supports_tools=False)
    req = LLMRequest(
        messages=(Message.user("hi"),),
        tools=(ToolSchema(name="t", description="d", input_schema={"type": "object"}),),
    )
    compiled = compile_request(req, spec)
    assert compiled.tools_enabled is False
    assert compiled.warnings


def test_structured_strategy_degrades_by_capability():
    req = LLMRequest(messages=(Message.user("hi"),), response_schema={"type": "object"})
    assert compile_request(req, _spec(supports_json_schema=True)).structured == "json_schema"
    assert (
        compile_request(req, _spec(supports_json_schema=False, supports_json_mode=True)).structured
        == "json_mode"
    )
    assert (
        compile_request(req, _spec(supports_json_schema=False, supports_json_mode=False)).structured
        == "prompt_only"
    )


# ---------------------------------------------------------------------------
# provider wire parsing (mocked transport, no network)
# ---------------------------------------------------------------------------
def test_openai_compat_parses_reasoning_and_tool_calls():
    body = {
        "id": "resp_1",
        "model": "o4-mini",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "reasoning_content": "the user asked for a plan; call the planner",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "rhythm_plan_preview", "arguments": '{"user_id": "hana", "day": "2026-10-26"}'},
                }],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                  "completion_tokens_details": {"reasoning_tokens": 30}},
    }
    provider = OpenAICompatProvider(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    spec = resolve("openai/o4-mini")
    compiled = compile_request(LLMRequest(messages=(Message.user("hi"),)), spec)
    out = provider.complete(LLMRequest(messages=(Message.user("hi"),)), spec, compiled)

    assert out.finish_reason == "tool_calls"
    assert out.reasoning is not None and "plan" in out.reasoning.text
    assert out.tool_calls[0].name == "rhythm_plan_preview"
    assert out.tool_calls[0].arguments["user_id"] == "hana"
    assert out.usage.reasoning_tokens == 30


def test_openai_compat_forbidden_roundtrip_never_echoes_reasoning():
    """DeepSeek-style: reasoning_content must not appear in follow-up messages."""
    spec = resolve("deepseek/deepseek-r1")
    req = LLMRequest(
        messages=(
            Message.user("hi"),
            Message.assistant(reasoning=ReasoningPart(text="thought hard")),
            Message.assistant("and the answer"),
            Message(Role.TOOL, (ToolResultPart(call_id="c1", content="{}"),)),
        ),
    )
    provider = OpenAICompatProvider(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [], "usage": {}}))
    )
    payload = provider._payload(req, spec, compile_request(req, spec))
    flat = json.dumps(payload["messages"])
    assert "thought hard" not in flat


def test_anthropic_thinking_roundtrips_signature_and_omits_temperature():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_1", "model": "claude-sonnet-4-5", "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "let me plan", "signature": "sig-abc"},
                {"type": "text", "text": "Here is your plan."},
            ],
            "usage": {"input_tokens": 80, "output_tokens": 60},
        })

    spec = resolve("anthropic/claude-sonnet-4-5")
    req = LLMRequest(
        messages=(
            Message.system("You are Rhythm."),
            Message.user("plan my day"),
            Message.assistant(reasoning=ReasoningPart(text="let me plan", signature="sig-abc")),
            Message(Role.TOOL, (ToolResultPart(call_id="toolu_1", content="{}"),)),
        ),
        temperature=0.4,
        reasoning_budget_tokens=1024,
        max_output_tokens=2048,
    )
    compiled = compile_request(req, spec)
    assert compiled.temperature is None  # thinking mode requires default temperature

    provider = AnthropicProvider(transport=httpx.MockTransport(handler))
    out = provider.complete(req, spec, compiled)

    assert seen["thinking"]["budget_tokens"] == 1024
    assert "temperature" not in seen
    # the assistant turn replayed the signed thinking block verbatim
    assistant = next(m for m in seen["messages"] if m["role"] == "assistant")
    thinking = [b for b in assistant["content"] if b["type"] == "thinking"]
    assert thinking == [{"type": "thinking", "thinking": "let me plan", "signature": "sig-abc"}]
    # response carries the reasoning back out, signature intact
    assert out.reasoning is not None and out.reasoning.signature == "sig-abc"
    assert out.text == "Here is your plan."


def test_gemini_thinking_budget_and_thought_parts():
    body = {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [
                {"text": "internal plan", "thought": True},
                {"text": "Here is your plan."},
                {"functionCall": {"name": "get_day_shape", "args": {"day": DAY.isoformat()}}},
            ]},
        }],
        "usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 40, "thoughtsTokenCount": 25},
    }
    provider = GeminiProvider(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    spec = resolve("google/gemini-2.5-flash")
    req = LLMRequest(messages=(Message.user("plan"),), reasoning_budget_tokens=512)
    out = provider.complete(req, spec, compile_request(req, spec, task="coach"))

    assert out.reasoning is not None and out.reasoning.redacted  # summary only, never raw CoT
    assert out.tool_calls[0].name == "get_day_shape"
    assert out.usage.reasoning_tokens == 25


# ---------------------------------------------------------------------------
# routing: fallback, budget, escalation
# ---------------------------------------------------------------------------
class FakeProvider:
    name = "openai_compat"

    def __init__(self, behave):
        self.behave = behave  # callable(spec_id, compiled) -> LLMResponse | raises

    def complete(self, req, spec, compiled):
        return self.behave(spec.id, compiled)


def _resp(model="m", finish="stop", usage=None) -> LLMResponse:
    return LLMResponse(
        message=Message.assistant("ok"), model=model, provider="openai_compat",
        finish_reason=finish, usage=usage or Usage(),
    )


def test_router_falls_back_when_primary_fails():
    calls = []

    def behave(model_id, compiled):
        calls.append(model_id)
        if model_id.endswith("mini"):
            from rhythm.llm.providers.base import ProviderError
            raise ProviderError("provider_unavailable", "503", retryable=True)
        return _resp(model=model_id)

    router = Router(
        providers={
            "openai_compat": FakeProvider(behave),
            "anthropic": FakeProvider(behave),
        }
    )
    out = router.complete("coach", LLMRequest(messages=(Message.user("hi"),)))
    assert "mini" not in out.model  # moved past the small model
    assert calls


def test_router_escalates_to_reasoning_on_length():
    def behave(model_id, compiled):
        if "haiku" in model_id:
            return _resp(model=model_id, finish="length")
        return _resp(model=model_id, finish="stop")

    router = Router(providers={"openai_compat": FakeProvider(behave), "anthropic": FakeProvider(behave)})
    out = router.complete("coach", LLMRequest(messages=(Message.user("plan my day"),)))
    assert "sonnet" in out.model  # escalated to the deep tier


def test_budget_exceeded_degrades_to_deterministic_signal():
    fp = FakeProvider(lambda *a: _resp())
    router = Router(
        providers={"openai_compat": fp, "anthropic": fp},
        budget=Budget(limit_usd_per_day=0.0001, spent_usd=0.0),
    )
    with pytest.raises(BudgetExceeded):
        router.complete("review", LLMRequest(messages=(Message.user("x" * 4000),)))


def test_router_exhaustion_raises_with_reasons():
    from rhythm.llm.providers.base import ProviderError as PE

    def behave(model_id, compiled):
        raise PE("provider_unavailable", "down", retryable=True)

    PE = PE = PE  # placeholder to keep flake calm
    from rhythm.llm.providers.base import ProviderError as _PE

    class Boom(FakeProvider):
        def complete(self, req, spec, compiled):
            raise _PE("provider_unavailable", "down", retryable=True)

    router = Router(providers={"openai_compat": Boom(None), "anthropic": Boom(None)})
    with pytest.raises(RoutingExhausted):
        router.complete("coach", LLMRequest(messages=(Message.user("hi"),)))


# ---------------------------------------------------------------------------
# structured output + planner bridge
# ---------------------------------------------------------------------------
class ScriptedRouter(Router):
    """A Router whose provider returns a canned JSON payload."""

    def __init__(self, payload: dict, *, reasoning: str = "", fail_first: int = 0):
        self.payload = payload
        self.reasoning = reasoning
        self.fail_first = fail_first
        self.calls = 0
        providers = {"openai_compat": self}
        providers["anthropic"] = self
        super().__init__(providers=providers)

    def complete(self, task, req, **kw):  # type: ignore[override]
        self.calls += 1
        if self.calls <= self.fail_first:
            return LLMResponse(
                message=Message.assistant("not json at all"),
                model="scripted", provider="openai_compat",
            )
        reasoning = ReasoningPart(text=self.reasoning) if self.reasoning else None
        return LLMResponse(
            message=Message.assistant(json.dumps(self.payload), reasoning=reasoning),
            model="scripted", provider="openai_compat",
        )


def test_structured_repairs_invalid_json():
    router = ScriptedRouter(DECISION, fail_first=1)
    out = request_structured(
        router, task="coach", schema={"type": "object"},
        messages=[Message.user("go")],
    )
    assert out.value["done"] is True
    assert out.attempts == 2


def test_structured_fails_after_repairs_exhausted():
    router = ScriptedRouter(DECISION, fail_first=99)
    with pytest.raises(StructuredOutputError):
        request_structured(
            router, task="coach", schema={"type": "object"},
            messages=[Message.user("go")], max_repairs=1,
        )


def _scene() -> Scene:
    return Scene(
        user_id="hana", day=DAY,
        at=DAY and __import__("datetime").datetime(2026, 10, 26, 9, 0),
        facts={"goal": "check in for today"},
        memory={}, available_tools=("rhythm.checkin", "rhythm.plan.preview"),
        persona="You are Rhythm.",
    )


def test_llm_planner_maps_model_output_to_agent_decision():
    payload = {
        "thought": "need the plan",
        "tool_calls": [{"name": "rhythm_plan_preview", "arguments": {"user_id": "hana", "day": DAY.isoformat()}}],
        "done": False,
    }
    router = ScriptedRouter(payload, reasoning="the user wants a plan")
    registry = build_registry(Runtime())
    planner = LLMPlanner(router, registry)

    decision = planner.decide(_scene(), [])

    assert decision.done is False
    assert decision.tool_calls[0].name == "rhythm.plan.preview"  # mapped back to registry name
    assert planner.last_reasoning == "the user wants a plan"      # kept for the trace


def test_llm_planner_filters_tools_the_principal_cannot_call():
    payload = {
        "thought": "sneaky",
        "tool_calls": [{"name": "rhythm_commitment_create", "arguments": {}}],
        "done": False,
    }
    router = ScriptedRouter(payload)
    planner = LLMPlanner(router, build_registry(Runtime()))
    decision = planner.decide(_scene(), [])
    assert decision.tool_calls == ()  # not in available_tools → dropped


class NativeToolRouter(ScriptedRouter):
    """Stands in for a reasoning model that answers with native tool calls."""

    def __init__(self, tool_name: str, arguments: dict, reasoning: str = "internal plan"):
        super().__init__({})
        self.tool_name = tool_name
        self.arguments = arguments
        self._reasoning_text = reasoning

    def complete(self, task, req, **kw):  # type: ignore[override]
        from rhythm.llm.messages import ReasoningPart as RP, ToolCallPart as TCP

        self.calls += 1
        return LLMResponse(
            message=Message.assistant(
                reasoning=RP(text=self._reasoning_text),
            ).__class__(
                Role.ASSISTANT,
                (RP(text=self._reasoning_text), ToolCallPart(id="t1", name=self.tool_name, arguments=self.arguments)),
            ),
            model="anthropic/claude-sonnet-4-5",
            provider="anthropic",
            finish_reason="tool_calls",
            usage=Usage(input_tokens=50, output_tokens=20, reasoning_tokens=64),
        )


def test_llm_planner_maps_native_tool_calls_from_a_reasoning_model():
    router = NativeToolRouter(
        "rhythm_plan_preview", {"user_id": "hana", "day": DAY.isoformat()},
        reasoning="the plan lives in the planner tool",
    )
    planner = LLMPlanner(router, build_registry(Runtime()))
    decision = planner.decide(_scene(), [])

    assert decision.done is False
    assert decision.tool_calls[0].name == "rhythm.plan.preview"
    assert "planner tool" in planner.last_reasoning
    # reasoning must never ride along as the answer
    assert "internal plan" not in decision.answer


def test_llm_planner_plain_text_after_work_is_the_final_answer():
    class DoneRouter(ScriptedRouter):
        def complete(self, task, req, **kw):  # type: ignore[override]
            return LLMResponse(
                message=Message.assistant("You are on track — same walk again today."),
                model="mid", provider="openai_compat",
            )

    planner = LLMPlanner(DoneRouter({}), build_registry(Runtime()))
    obs = Observation(
        call=ToolCall(name="rhythm.checkin", arguments={}), ok=True, result={"phase": "checked_in"}
    )
    decision = planner.decide(_scene(), [obs])
    assert decision.done is True
    assert decision.answer.startswith("You are on track")


def test_llm_planner_degrades_when_model_skips_tools_before_work():
    class LazyRouter(ScriptedRouter):
        def complete(self, task, req, **kw):  # type: ignore[override]
            return LLMResponse(
                message=Message.assistant("totally unparseable"),
                model="mid", provider="openai_compat",
            )

    planner = LLMPlanner(LazyRouter({}), build_registry(Runtime()))
    decision = planner.decide(_scene(), [])  # no transcript: it did no work
    assert planner.last_degraded is True
    assert decision.tool_calls and decision.tool_calls[0].name == "rhythm.checkin"


def test_llm_planner_degrades_to_scripted_when_model_fails():
    router = ScriptedRouter(DECISION, fail_first=99)
    planner = LLMPlanner(router, build_registry(Runtime()))
    decision = planner.decide(_scene(), [])
    assert planner.last_degraded is True
    assert "degraded" in decision.thought
    # the scripted planner still produces a valid next step for the loop
    assert decision.tool_calls and decision.tool_calls[0].name == "rhythm.checkin"


def test_reasoning_never_reaches_the_user_and_is_digested_in_traces():
    from rhythm.agent.trace import digest

    payload = {"thought": "t", "tool_calls": [], "done": True, "answer": "Here is your plan."}
    router = ScriptedRouter(payload, reasoning="SECRET chain of thought")
    planner = LLMPlanner(router, build_registry(Runtime()))
    decision = planner.decide(_scene(), [])

    # The user-facing surface is clean.
    assert "SECRET" not in decision.answer
    # The live copy exists in memory for round-trip...
    assert "SECRET" in planner.last_reasoning
    # ...and the thought is marked private so the loop digests it in traces.
    assert decision.thought_private is True
    assert decision.thought == "t"  # the JSON payload's own thought field
    assert planner.last_reasoning == "SECRET chain of thought"  # provider CoT, in-memory only


def test_agent_loop_digests_model_cot_in_steps():
    payload = {"thought": "t", "tool_calls": [], "done": True, "answer": "Here is your plan."}
    router = ScriptedRouter(payload, reasoning="SECRET chain of thought")
    registry = build_registry(Runtime())
    agent = Agent(registry=registry, runtime=Runtime(), model=LLMPlanner(router, registry))

    result = agent.run(Mission("check in for today", "hana", DAY, raw_text="fine"))
    think_steps = [s for s in result.steps if s.kind == "think"]
    assert think_steps, "loop recorded think steps"
    joined = " ".join(s.detail for s in think_steps)
    assert "SECRET" not in joined
    assert "reasoning digest" in joined
