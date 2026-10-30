"""Model capability descriptors.

The design rule that makes "any latest model" work:

> **Never branch on a model name. Branch on a capability.**

Every provider/model difference that matters to the agent is a field here, and the
request compiler (`compiler.py`) reads only these fields. Adding a new model is a
registry entry, not a code path.

Reasoning models are the reason this exists. They are not "better models with the
same API":

- they reject sampling params (`temperature`, `top_p`, `presence_penalty`, `stop`)
  and use `max_completion_tokens`;
- they bill reasoning tokens as output, often at the output rate;
- their thinking must be **round-tripped** across tool calls, and the rules differ
  per vendor (Anthropic wants the signed block back; DeepSeek forbids echoing it).

Costs below are indicative planning numbers, not quotes. Verify against provider
pricing before they drive any commercial decision.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional


class ReasoningStyle(str, Enum):
    NONE = "none"         # classic instruct model
    EFFORT = "effort"     # reasoning_effort: minimal|low|medium|high
    BUDGET = "budget"     # thinking budget in tokens
    TOGGLE = "toggle"     # on/off (enable_thinking)


class Roundtrip(str, Enum):
    NONE = "none"            # do not send reasoning back
    SIGNATURE = "signature"  # must return the signed block verbatim
    OPAQUE = "opaque"        # return an encrypted reasoning token
    FORBIDDEN = "forbidden"  # echoing it back is an error


class Latency(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str                      # openai | anthropic | google | openai_compat
    family: str = ""
    context_window: int = 128_000
    max_output: int = 8_192

    # tools & structure
    supports_tools: bool = True
    parallel_tool_calls: bool = True
    strict_tool_schema: bool = False
    supports_json_schema: bool = True
    supports_json_mode: bool = True

    # sampling params a classic model accepts and a reasoning model may not
    supports_temperature: bool = True
    supports_top_p: bool = True
    supports_stop: bool = True

    # reasoning behaviour
    reasoning: ReasoningStyle = ReasoningStyle.NONE
    reasoning_roundtrip: Roundtrip = Roundtrip.NONE
    max_reasoning_effort: str = "high"
    supports_reasoning_summary: bool = False

    # misc
    supports_vision: bool = False
    max_tokens_param: str = "max_tokens"  # or max_completion_tokens
    latency: Latency = Latency.BALANCED

    # indicative USD per million tokens
    cost_in_per_mtok: float = 1.0
    cost_out_per_mtok: float = 4.0
    cost_reasoning_per_mtok: float = 4.0

    verified: bool = True

    def cost(self, input_tokens: int, output_tokens: int, reasoning_tokens: int = 0) -> float:
        return round(
            input_tokens / 1_000_000 * self.cost_in_per_mtok
            + output_tokens / 1_000_000 * self.cost_out_per_mtok
            + reasoning_tokens / 1_000_000 * self.cost_reasoning_per_mtok,
            6,
        )


# ---------------------------------------------------------------------------
# Seed registry. Keys are "provider/family"; `resolve` also matches bare ids.
# ---------------------------------------------------------------------------
KNOWN: dict[str, ModelSpec] = {
    # --- OpenAI -----------------------------------------------------------
    "openai/gpt-5": ModelSpec(
        id="openai/gpt-5", provider="openai", family="gpt-5",
        reasoning=ReasoningStyle.EFFORT, reasoning_roundtrip=Roundtrip.OPAQUE,
        supports_temperature=False, supports_top_p=False, supports_stop=False,
        max_tokens_param="max_completion_tokens", supports_json_schema=True,
        latency=Latency.DEEP, cost_in_per_mtok=1.25, cost_out_per_mtok=10.0,
        cost_reasoning_per_mtok=10.0,
    ),
    "openai/o4-mini": ModelSpec(
        id="openai/o4-mini", provider="openai", family="o4",
        reasoning=ReasoningStyle.EFFORT, reasoning_roundtrip=Roundtrip.OPAQUE,
        supports_temperature=False, supports_top_p=False, supports_stop=False,
        max_tokens_param="max_completion_tokens",
        latency=Latency.BALANCED, cost_in_per_mtok=1.1, cost_out_per_mtok=4.4,
        cost_reasoning_per_mtok=4.4,
    ),
    "openai/gpt-4.1": ModelSpec(
        id="openai/gpt-4.1", provider="openai", family="gpt-4.1",
        latency=Latency.BALANCED, cost_in_per_mtok=2.0, cost_out_per_mtok=8.0,
    ),
    "openai/gpt-4.1-mini": ModelSpec(
        id="openai/gpt-4.1-mini", provider="openai", family="gpt-4.1",
        latency=Latency.FAST, cost_in_per_mtok=0.4, cost_out_per_mtok=1.6,
    ),
    # --- Anthropic --------------------------------------------------------
    "anthropic/claude-sonnet-4-5": ModelSpec(
        id="anthropic/claude-sonnet-4-5", provider="anthropic", family="claude-sonnet-4",
        reasoning=ReasoningStyle.BUDGET, reasoning_roundtrip=Roundtrip.SIGNATURE,
        supports_temperature=False, supports_stop=True,
        max_output=64_000, latency=Latency.BALANCED,
        cost_in_per_mtok=3.0, cost_out_per_mtok=15.0, cost_reasoning_per_mtok=15.0,
    ),
    "anthropic/claude-haiku-4-5": ModelSpec(
        id="anthropic/claude-haiku-4-5", provider="anthropic", family="claude-haiku-4",
        reasoning=ReasoningStyle.BUDGET, reasoning_roundtrip=Roundtrip.SIGNATURE,
        supports_temperature=False, supports_stop=True,
        latency=Latency.FAST, cost_in_per_mtok=1.0, cost_out_per_mtok=5.0,
        cost_reasoning_per_mtok=5.0,
    ),
    # --- Google -----------------------------------------------------------
    "google/gemini-2.5-pro": ModelSpec(
        id="google/gemini-2.5-pro", provider="google", family="gemini-2.5-pro",
        reasoning=ReasoningStyle.BUDGET, reasoning_roundtrip=Roundtrip.SIGNATURE,
        supports_temperature=False, supports_stop=True,
        max_output=65_536, latency=Latency.DEEP,
        cost_in_per_mtok=1.25, cost_out_per_mtok=10.0, cost_reasoning_per_mtok=10.0,
    ),
    "google/gemini-2.5-flash": ModelSpec(
        id="google/gemini-2.5-flash", provider="google", family="gemini-2.5-flash",
        reasoning=ReasoningStyle.BUDGET, reasoning_roundtrip=Roundtrip.NONE,
        supports_temperature=True, latency=Latency.FAST,
        cost_in_per_mtok=0.3, cost_out_per_mtok=2.5, cost_reasoning_per_mtok=2.5,
    ),
    # --- OpenAI-compatible servers (vLLM / SGLang / Ollama / Together …) ---
    "deepseek/deepseek-r1": ModelSpec(
        id="deepseek/deepseek-r1", provider="openai_compat", family="deepseek-r1",
        reasoning=ReasoningStyle.NONE, reasoning_roundtrip=Roundtrip.FORBIDDEN,
        supports_temperature=False, supports_stop=False,
        latency=Latency.DEEP, cost_in_per_mtok=0.55, cost_out_per_mtok=2.19,
    ),
    "qwen/qwen3-235b": ModelSpec(
        id="qwen/qwen3-235b", provider="openai_compat", family="qwen3",
        reasoning=ReasoningStyle.TOGGLE, reasoning_roundtrip=Roundtrip.FORBIDDEN,
        supports_temperature=True, latency=Latency.BALANCED,
        cost_in_per_mtok=0.2, cost_out_per_mtok=0.6,
    ),
    "meta/llama-3.3-70b": ModelSpec(
        id="meta/llama-3.3-70b", provider="openai_compat", family="llama-3.3",
        latency=Latency.FAST, cost_in_per_mtok=0.2, cost_out_per_mtok=0.6,
    ),
    "local/vllm": ModelSpec(
        id="local/vllm", provider="openai_compat", family="local",
        reasoning=ReasoningStyle.TOGGLE, reasoning_roundtrip=Roundtrip.FORBIDDEN,
        supports_temperature=True, latency=Latency.BALANCED,
        cost_in_per_mtok=0.0, cost_out_per_mtok=0.0, cost_reasoning_per_mtok=0.0,
    ),
}


def _load_overrides() -> dict[str, ModelSpec]:
    """`RHYTHM_MODEL_SPECS=/path/specs.json` adds or replaces entries.

    Lets an operator register a model the day it ships, without a release.
    """
    path = os.environ.get("RHYTHM_MODEL_SPECS")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out: dict[str, ModelSpec] = {}
    for key, body in raw.items():
        base = KNOWN.get(key) or ModelSpec(id=key, provider=body.get("provider", "openai_compat"))
        out[key] = replace(base, **body)
    return out


def all_specs() -> dict[str, ModelSpec]:
    merged = dict(KNOWN)
    merged.update(_load_overrides())
    return merged


UNVERIFIED_DEFAULT = ModelSpec(
    id="unverified",
    provider="openai_compat",
    # Fail closed: we do not guess what an unknown model accepts.
    supports_tools=False,
    supports_json_schema=False,
    supports_json_mode=True,
    supports_temperature=False,
    supports_top_p=False,
    supports_stop=False,
    reasoning=ReasoningStyle.NONE,
    reasoning_roundtrip=Roundtrip.NONE,
    verified=False,
)


def resolve(model_id: str) -> ModelSpec:
    """Exact id → family prefix → conservative default (flagged unverified).

    Prefix matching is deliberate: a new point release (`gpt-5.1`) should inherit
    its family's capabilities instead of silently falling back to the safest spec.
    """
    specs = all_specs()
    if model_id in specs:
        return specs[model_id]

    if "/" not in model_id:
        for key, spec in specs.items():
            if key.split("/", 1)[1] == model_id:
                return spec

    best: Optional[ModelSpec] = None
    best_len = 0
    provider_prefix, _, bare = model_id.partition("/")
    if not _:  # bare id like "gpt-5-turbo": no provider restriction
        provider_prefix, bare = "", model_id
    for key, spec in specs.items():
        family = spec.family or key.split("/", 1)[-1]
        provider_ok = not provider_prefix or provider_prefix == spec.provider
        if provider_ok and bare.startswith(family):
            if len(family) > best_len:
                best, best_len = spec, len(family)
    if best is not None:
        return replace(best, id=model_id)

    return replace(UNVERIFIED_DEFAULT, id=model_id)
