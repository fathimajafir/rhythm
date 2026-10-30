# Model portability & reasoning models

**Companion to** design doc §5 (model selection). **Status:** implemented in `src/rhythm/llm/`.

---

## 1. The requirement, stated precisely

"Use any latest model" is easy to say and has three hard versions:

1. **Swap vendors** without touching agent code (OpenAI ↔ Anthropic ↔ Gemini ↔ vLLM).
2. **Adopt a new model the day it ships**, including one whose API rejects things
   every previous model accepted.
3. **Use reasoning models well** — not as "the big model", but with their actual
   economics and mechanics: thinking budgets, round-tripped reasoning across tool
   calls, and escalation only when a step earns it.

Reasoning models are the forcing function, because they are *not* backwards
compatible with the classic API surface:

| Difference | Classic instruct | Reasoning model |
|---|---|---|
| `temperature`, `top_p`, `stop` | supported | often **rejected** outright |
| output cap | `max_tokens` | `max_completion_tokens` (OpenAI) |
| thinking | none | a **budget** (Anthropic/Gemini) or an **effort** dial (OpenAI); billed as output tokens |
| tool loops | stateless messages | thinking must be **round-tripped** across turns — signed blocks (Anthropic), encrypted items (OpenAI), or **forbidden to echo** (DeepSeek/Qwen) |
| structured output | `json_schema` strict | varies; may need `json_mode` + validation + repair |

So portability cannot be a model name in a config file. It is a **capability layer**.

## 2. The architecture: branch on capability, never on name

```
agent loop ──> Model protocol ──> LLMPlanner ──> Router ──> Provider ──> wire
                 (unchanged)         │             │           │
                                    DECISION      routes +    capability-
                                    schema        budget      correct payload
```

| Layer | File | Responsibility |
|---|---|---|
| **Content model** | `llm/messages.py` | Neutral messages where **reasoning is a first-class part** (`ReasoningPart` with signature/opaque/redacted), not text |
| **Capability registry** | `llm/capabilities.py` | One `ModelSpec` per model: tools, JSON-schema support, sampling-param tolerance, reasoning style (`effort`/`budget`/`toggle`/none), round-trip rule, latency class, indicative costs |
| **Request compiler** | `llm/compiler.py` | Reads only the spec: drops `temperature` when the model won't take it, maps effort/budget, clamps output, picks the structured-output strategy — and records **why** in notes/warnings |
| **Providers** | `llm/providers/*` | Dumb wire adapters: OpenAI-compatible (OpenAI, vLLM, SGLang, Ollama, Together, Groq, OpenRouter), Anthropic, Gemini. Parse reasoning/tool parts back into the neutral model |
| **Router** | `llm/router.py` | Task → capability-tier chain, fallbacks, per-tenant spend budget (reasoning tokens counted), escalation to the deep tier |
| **Structured** | `llm/structured.py` | Schema-validated output with a bounded repair loop — the *guarantee* layer when native JSON-schema is missing |
| **Bridge** | `llm/adapters.py` | `LLMPlanner` implements the existing agent `Model` protocol. The agent loop itself did not change |

## 3. The rules that make reasoning models safe here

1. **Reasoning is a first-class part, never an answer.** `ReasoningPart` is carried
   for round-trip and trace only. It never reaches the user, never substitutes for
   a missing tool result, and is never quoted by the coach (test:
   `test_reasoning_never_leaks_into_the_answer`).
2. **Round-trip is per-vendor, from the spec.** Anthropic gets its signed thinking
   block back verbatim; OpenAI gets the opaque item; DeepSeek/Qwen never see their
   `reasoning_content` again. One `Roundtrip` enum, no vendor ifs in the agent.
3. **Escalation, not default.** Routes name capability tiers — `small` (fast),
   `mid` (balanced), `deep` (reasoning). The daily coach loop runs on `mid` and
   escalates to `deep` only when a step hits `length` or fails. Triage **never**
   escalates: high volume + narrow labels + reasoning pricing is how budgets die.
4. **Reasoning budgets are per task.** `DEFAULT_BUDGET_BY_TASK` keeps the daily
   loop bounded (coach: 1,024; review: 2,048; deep_plan: 4,096 tokens) and the
   compiler guarantees the answer always has headroom after thinking.
5. **Reasoning tokens are output tokens.** The budget counts them at the reasoning
   rate, and `BudgetExceeded` degrades to the deterministic planner instead of
   failing the user — a coaching miss costs less than an outage.
6. **Deterministic work stays deterministic.** Arithmetic, planning eligibility and
   safety routing are untouched (design §3). A reasoning model may *reason about*
   the plan; it may not *make up* the plan's numbers.
7. **New model → registry entry, not code path.** `RHYTHM_MODEL_SPECS=/path.json`
   registers a capability spec operationally. An unregistered model runs with
   **conservative, unverified** capabilities (no tools, no sampling params) and a
   loud warning — fail closed, not slow.

## 4. How a new model gets adopted (the whole procedure)

```jsonc
// RHYTHM_MODEL_SPECS=/etc/rhythm/specs.json
{
  "newvendor/nova-3": {
    "provider": "openai_compat",           // or "anthropic" / "google"
    "reasoning": "budget",                  // none | effort | budget | toggle
    "reasoning_roundtrip": "signature",
    "supports_temperature": false,
    "supports_tools": true,
    "supports_json_schema": true,
    "max_output": 32000,
    "latency": "balanced",
    "cost_in_per_mtok": 1.0, "cost_out_per_mtok": 5.0,
    "verified": true
  }
}
```

Then point the route at it:

```python
router.model_overrides["mid"] = "newvendor/nova-3"
```

No agent code changes. The drift tests and eval suite (§ design doc §10) decide
whether it *stays*.

## 5. What is verified by tests (`tests/test_llm.py`, 24 cases)

- capability resolution: exact, family prefix (a `gpt-5.1` point release inherits
  its family), unknown → fail-closed spec, and operational overrides;
- compilation: reasoning models drop `temperature`/`stop` and rename
  `max_tokens`; effort clamping; thinking budgets keep headroom for the answer;
  structured-output strategy degradation (`json_schema` → `json_mode` → prompt);
- wire parsing over mocked HTTP for all three adapters, including
  `reasoning_content`, signed thinking round-trip, `thought` summaries, and
  reasoning-token usage;
- router: fallback on provider failure, budget exhaustion, reasoning escalation;
- structured output: JSON extraction, schema validation, bounded repair;
- the agent loop running unchanged on a routed model — including that a model
  failure **degrades to the scripted planner** mid-flow instead of crashing, and
  that chain-of-thought never reaches a user-facing answer.

## 6. Chain-of-thought containment (reasoning-model policy)

Reasoning models make one product risk sharper: the model's private reasoning can
contain the user's own words, health inferences, or worse. Rhythm's policy is
enforced in code, not in prompts:

| Surface | What model CoT may do there |
|---|---|
| `LLMPlanner.last_reasoning` (in-memory) | ✅ full text — needed for provider round-trip (Anthropic signature) |
| `Decision.thought` | ✅ marked `thought_private=True` — exists so the loop can reason about it |
| `AgentResult.steps` / traces | ❌ **digest only** (`reasoning digest a1b2… (n chars)`) |
| User-facing `answer` | ❌ never — tested |
| Logs, MCP results, employer surfaces | ❌ never — redaction + the data wall (§11.1) |

So "reasoning models" are safe here in the strict sense: the thinking exists, does
its job across tool calls, and **cannot leave the process in clear text**.

## 7. Deliberate boundaries

- **No vendor SDK imports anywhere in the agent.** Providers talk HTTP with
  `httpx`; swapping a vendor never touches the loop.
- **Streaming is not implemented yet** (Phase 2 UX work). The message model is
  streaming-ready (parts), and reasoning models make streaming *visible* thinking
  a product decision (show a "thinking…" state, never the raw CoT) — that belongs
  to the coach UX, not this layer.
- **The judge (§ evaluate) is model-agnostic by design**: it scores answers, not
  providers, so model-matrix evaluation is "run the suite per route", not new code.
