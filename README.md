# Rhythm

A daily energy-and-recovery companion: **one action a day**, chosen from the person's
actual day, closed without shame. The design is in [`docs/design/`](docs/design/).

This repo is at **Phase 1: the loop skeleton** — the whole product loop working
end-to-end with **zero model calls**. That is deliberate. If the loop is not
correct and auditable on its own, no model will save it.

```
morning check-in  →  commit one action  →  nudge in the window
        ↑                                          ↓
   weekly review  ←  close the day (1 tap)  ←  observe outcome
```

## Quickstart

```bash
make install     # venv + deps
make test        # 36 tests, no network, no model
make demo        # a full day loop in the terminal
make run         # http://localhost:8099/docs
```

## What is implemented

| Piece | Module | Notes |
|---|---|---|
| Day state machine | `loop.py` | `IDLE → CHECKED_IN → COMMITTED → NUDGED → CLOSED` |
| Deterministic metrics | `metrics.py` | Sleep midpoint, circular regularity, debt, energy band, effort ceiling |
| Signal normalisation | `signals.py` | HealthKit + aggregator → one schema; **gaps stay `None`, never `0`** |
| Action library | `library.py` | 14 actions, each with a **time window** and a bad-day fallback |
| Planner | `planner.py` | Constrained optimiser; emits exactly one primary + one optional |
| Templated coach | `coach.py` | EN + AR authored separately, ≤ 60 words |
| Safety router | `safety.py` | Deterministic red flags, fails closed, opens escalation |
| HTTP surface | `api.py` | FastAPI, in-memory store |
| Idempotency | `loop.py` | Replayed closes return the original result |

## Design rules that are enforced in code

These are the parts worth reviewing, because they are the design doc made executable:

- **All arithmetic is Python.** The planner and metrics never call a model. (`metrics.py`)
- **A gap is unknown, not zero.** Missing sleep yields `None` and `confidence=unknown`,
  and the coach says so instead of guessing. (`signals.py`, `coach.unknown_data_note`)
- **Minimum-n before any trend claim.** Fewer than 4 nights → no regularity number;
  fewer than 3 logged days → the review refuses to quote a percentage. (`metrics.py`, `coach.py`)
- **One action, never a menu.** The planner returns a single primary; the coach may
  negotiate *down*, never invent. (`planner.py`)
- **Windows, not just slots.** "Get up at your usual time" is withdrawn at 09:05 and
  never nudged at 13:00 — the bug the demo caught on its first run. (`planner.in_window`)
- **Titles carry no duration.** A bad day changes the minutes, so the copy cannot
  contradict them.
- **Red flags never reach a generator.** The router runs first and returns a fixed
  protocol; the plan is blocked and an escalation opens in the same breath. (`safety.py`)

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Probe; reports `model_calls: 0` |
| `POST` | `/v1/users` | Register a profile (locale, constraints, targets) |
| `POST` | `/v1/checkin` | Fold sleep/energy/signals into today's state |
| `GET` | `/v1/today/{user_id}?day=` | State + plan + proposal, or the check-in prompt |
| `POST` | `/v1/commitments?user_id=&day=` | Create the one commitment |
| `POST` | `/v1/commitments/{id}/close` | `done \| partial \| miss`, idempotent |
| `POST` | `/v1/plan/replan` | Re-plan with a stated reason |
| `GET` | `/v1/review/weekly/{user_id}?day=` | Deterministic weekly synthesis |
| `POST` | `/v1/safety/classify` | Standalone safety band |

## Agent layer (Phase 1.5)

`src/rhythm/agent/` adds the full agentic architecture from Google's *Introduction
to Agents* whitepaper, on top of the deterministic core:

| Piece | Module |
|---|---|
| 5-step loop: Mission → Scene → Think → Act → Observe | `agent/agent.py` |
| Data contracts for each step | `agent/contracts.py` |
| Tool registry — one definition, served twice (agent + MCP) | `agent/registry.py`, `agent/tools.py` |
| Agent identity, scopes, subject binding | `agent/policy.py` |
| OTel-shaped traces with PII redaction | `agent/trace.py` |
| Model boundary (scripted planner now, LLM later) | `agent/model.py` |
| AgentOps: eval cases, go/no-go, LM-judge rubric | `agent/evaluate.py` |
| **Model portability layer** — any vendor, any reasoning model | `llm/` |
| Capability registry (branch on capability, never on model name) | `llm/capabilities.py` |
| Request compiler (drops `temperature` for reasoning models, maps thinking budgets) | `llm/compiler.py` |
| Wire adapters: OpenAI-compatible, Anthropic, Gemini | `llm/providers/` |
| Router: task tiers, fallbacks, spend budget, reasoning escalation | `llm/router.py` |
| Schema-validated output with repair loop | `llm/structured.py` |
| CoT containment: reasoning never leaves the process in clear text | `agent/agent.py`, `llm/adapters.py` |

```bash
PYTHONPATH=src .venv/bin/python -c "
from datetime import date
from rhythm.agent import Agent, build_registry, Runtime, Mission, run_eval
rt = Runtime()
agent = Agent(registry=build_registry(rt), runtime=rt)
print(agent.run(Mission('check in for today', 'hana', date(2026,10,26))).answer)
print(run_eval(lambda: Agent(registry=build_registry(Runtime()), runtime=Runtime())).summary())
"
```

The capability level is **Level 2 (strategic problem-solver)** — planning, memory,
tools, context engineering. Deliberately not Level 3: one accountable loop with a
human clinician backstop until the pilot proves the loop works.

## MCP server

`mcp/` exposes the same tool registry over the Model Context Protocol — tools,
resources, prompts, bearer-token identity with per-token scopes, subject binding
against the confused-deputy attack, a hashed anti-shadowing manifest, and an error
channel written for the calling model. See `mcp/README.md` and
`docs/research/agent-principles.md` for the principle-by-principle mapping.

```bash
make mcp-http    # http://localhost:8090/mcp
make mcp-test
```

## Deliberately not here yet

No LLM, no database, no wearable SDK, no auth, no clinician console, no employer
aggregate. Each belongs to a later phase, and the design doc says why:
the loop first, then the coach, then signals, then the clinical tier.

The reporting layer is *absent on purpose* — the employer aggregate must never be
able to join to the individual store (design §11.1), and the cheapest way to
guarantee that is to not have the store reachable from it.

## Deployment (no Docker)

One stateful API + stateless MCP gateways + nginx, run as plain systemd services.
Units, env template, nginx config and backup script live in `deploy/`; the full
runbook (install, security model, backups, upgrades, scaling model, and the honest
"not production" list) is in `docs/deploy/production.md`.

```bash
make prod-api    # foreground API with a durable store
make backup      # timestamped snapshot + retention
```

## Layout

```
src/rhythm/     domain, metrics, signals, library, planner, coach, safety, loop, store, api
tests/          metrics · planner · loop · safety · API  (36 tests)
scripts/demo.py one full day, printed
docs/design/    the design document (v0.3)
docs/decisions/ ADR-001 pilot employer · ADR-002 clinical staffing
docs/ops/       pilot employer scorecard
deploy/         systemd units, env template, nginx, backup script
docs/deploy/    production runbook (no Docker)
docs/design/model-portability.md  any-model / reasoning-model architecture
mcp/            MCP server (tools, resources, prompts, security)
```

## Next milestone

Phase 2 (`weeks 5–6` in the plan): replace `coach.py` templates with the routed
coach — triage (small model) → coach (mid-tier) → output contract gate — leaving
`metrics.py` and `planner.py` untouched. The tests here become the regression
suite that proves the model did not change the loop's behaviour.
