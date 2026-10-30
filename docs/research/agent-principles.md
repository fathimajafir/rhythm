# Principles applied — from the two Kaggle/Google whitepapers

Sources (read in full; page references from the PDFs):

- **IA** — *Introduction to Agents*, Google/Kaggle, Nov 2025 (Alan Blount, Antonio Gulli, Shubham Saboo, Michael Zimmermann, Vladimir Vuskovic)
- **MCP** — *Agent Tools & Interoperability with MCP*, Google/Kaggle, updated May 2026

Each principle below is mapped to where it is **implemented in this repo**, not just
acknowledged. Principles deliberately deferred are marked with the reason.

---

## From "Introduction to Agents"

| # | Principle (IA) | Where it lives here |
|---|---|---|
| 1 | **An agent = Model + Tools + Orchestration** — "LMs in a loop with tools to accomplish an objective" (p.10) | `rhythm/agent/agent.py` (loop) + `rhythm/agent/model.py` (model seam) + `rhythm/agent/tools.py` (tools) |
| 2 | **The 5-step agentic process**: Get the Mission → Scan the Scene → Think It Through → Take Action → Observe and Iterate (p.10–12) | `agent/contracts.py` names the types (`Mission`, `Scene`, `Decision`, `Observation`); `agent/agent.py` runs the loop; each step is recorded |
| 3 | **Taxonomy Level 2 — strategic problem-solver**: multi-step planning, tools, memory, context engineering (p.16) | The agent plans a tool sequence, keeps a 7-day memory slice, curates context. Explicitly **not** Level 3 yet — one accountable loop with a human clinician backstop, until the pilot proves the loop |
| 4 | **Context engineering**: "actively select, package and manage the most relevant information for each step" (p.16) | `Agent._memory_slice` (last 7 attempts only), `model.build_prompt` (scene + calls so far, never the store), `policy.visible_tools` (the model never sees tools it cannot call) |
| 5 | **Instruct with domain knowledge and persona** (p.23) | `Agent.persona` — the Rhythm voice: one action, never a menu, no punishment, sourced claims only |
| 6 | **The orchestration layer owns planning, memory and when to think vs. call tools** (p.22) | `agent.py` owns the loop and budgets; `model.py` only decides; tools only act |
| 7 | **AgentOps: evaluate like an A/B experiment; quality via an LM judge, not pass/fail; metrics-driven go/no-go** (p.27–30) | `agent/evaluate.py`: deterministic cases, tool-success metric, explicit `go_no_go: GO/NO-GO`, and `JUDGE_RUBRIC` + `judge()` seam for the Phase-2 judge model |
| 8 | **Debug with OpenTelemetry traces** (p.30) | `agent/trace.py`: spans with parent links, durations, events, statuses — OTel-shaped, exportable |
| 9 | **Cherish human feedback** (p.31) | Clinician console is the human-in-the-loop tier (design §9.5); escalations recorded in `Runtime.loop.store.escalations` |
| 10 | **Agent identity: a new class of principal** with its own verifiable identity, distinct from the user's (p.35) | `agent/policy.py::Principal(agent_id, subject, scopes, audience)` — carried on every call, enforced in `Registry.call` |
| 11 | **Policies to constrain access; least privilege** (p.37) | `policy.authorize` (fail closed), `visible_tools` (least privilege applied to the tool list itself), per-tool scopes in each `ToolSpec` |
| 12 | **Governance: a control plane instead of sprawl** (p.40) | One `Registry` is the single definition of every tool — schema, scopes, annotations, version. The agent, the evaluator and the MCP server all read it; nothing else may define a tool |
| 13 | **Agents and humans / agents and agents interoperability** (p.31–34) | Humans: clinician escalation (red band blocks the plan). Agents: this repo's MCP server; A2A noted as future work |

**Deliberately deferred (with reasons):**

| IA concept | Why deferred |
|---|---|
| Level 3 multi-agent (coordinator/worker, debate) | One accountable loop + human backstop is auditable; a crew is not. Revisit after the pilot |
| Level 4 self-evolving / agent gym | Needs traffic and an outcome signal; the stepped-wedge pilot *is* the first gym |
| Agents and money | Explicit non-goal of v1 (design §1 non-goals) |

---

## From "Agent Tools & Interoperability with MCP"

| # | Principle (MCP paper) | Where it lives here |
|---|---|---|
| 1 | **Tools are the agent's eyes and hands**; design definitions for the *calling model* (p.6–7) | Every `ToolSpec` carries an LLM-facing description written for the caller, including warnings ("do not claim trends from low-confidence data") |
| 2 | **Full tool definitions: name, title, description, inputSchema — treat outputSchema as required; descriptions "should always be included"** (p.27) | `ToolSpec` (title+description always set); MCP adapters expose title/description/inputSchema; the drift test enforces agreement |
| 3 | **Annotations: readOnly / destructive / idempotent / openWorld hints** (p.28) | Set from the same `ToolSpec` the agent uses — the two surfaces cannot disagree (`test_no_schema_drift_between_registry_and_mcp`) |
| 4 | **Error handling: JSON-RPC protocol errors vs `isError` tool errors — "error messages are an important channel for providing context to the calling LLM"** (p.31) | `errors.as_tool_message`: every tool error carries `[code] message. Next step: hint` — e.g. `[idempotency_key_required] … Supply a caller-generated key so a retry cannot double-apply` |
| 5 | **Primitives: tools, resources, prompts** (p.25–33) | Tools (9), resources (`rhythm://manifest`, `rhythm://library/actions`), prompts (`daily_checkin`, `weekly_review` — EN/AR authored) |
| 6 | **Enterprise gap: authn/authz and identity propagation are not natively standardised** (p.40) | Closed app-side: bearer → `Principal`, audience binding, per-token scopes, subject binding on every call (`mcp/security.py`) |
| 7 | **Enterprise gap: no native observability** (p.40) | `Trace` spans per tool call; `/healthz`, `/readyz`; manifest endpoint |
| 8 | **Risk: tool shadowing** (p.45) | Hard namespacing (`rhythm.*`), versioned tool manifest with a digest at `rhythm://manifest` and `/.well-known/rhythm-manifest.json`; clients pin the digest |
| 9 | **Risk: malicious tool definitions / consumed contents** (p.47) | Tool outputs are data, never instructions: the coach's claims rule (design §9.4) + the safety router runs before any generation; the red band blocks the plan |
| 10 | **Risk: sensitive information leaks** (p.48) | `trace.redact` — args are digested, sensitive keys (`text`, `bedtime`, tokens…) never enter logs; employer layer cannot join to the individual store (design §11.1) |
| 11 | **Risk: no per-tool scope limiting** (p.49) — "invocation should use audience and scoped credentials… least privilege" | Audience check + per-token scopes + per-tool required scopes in `policy.authorize`; tokens are short-lived (`expires_at`) |
| 12 | **Risk: confused deputy** (p.51 appendix) | The subject is bound into the call: an agent credential for Hana cannot read Omar's state (`test_confused_deputy_is_denied`); credentials are never forwarded to backends |
| 13 | **Idempotency across a network boundary** | `rhythm.commitment.close` requires a caller-generated `idempotency_key`; replays return the original result (`test_full_loop_with_idempotent_replay`) |
| 14 | **Pagination for list tools** | `rhythm.action.library` exposes `limit`/`offset` + `next_offset` |

---

## The one architectural decision that makes this hold together

**Tools are defined exactly once** — in `rhythm/agent/tools.py`, as `ToolSpec`s with
JSON Schema, scopes and MCP annotations — and *served twice*: to the in-process
agent loop and to the MCP server. A schema-drift test fails the build if the two
surfaces ever disagree. That is what "scalable and reusable" means here: adding a
tool is one spec + one thin adapter + the drift test staying green.
