# Rhythm MCP server

The wellness agent's tools, exposed over the **Model Context Protocol** so any MCP
host (Claude Desktop, an IDE agent, another of our agents) can use them. Built on
the official `mcp` Python SDK (2.x).

The tools are **not defined here**. They are defined once in
`src/rhythm/agent/tools.py` (schema, scopes, annotations, handler) and this server
adapts them to MCP. A drift test fails the build if the two surfaces disagree —
that is what makes the tool set reusable instead of duplicated.

## Run

```bash
make mcp-test            # 13 tests: protocol, auth, idempotency, drift
make mcp-http            # http://localhost:8090 — /healthz /readyz /mcp
make mcp-stdio           # for MCP hosts that spawn a process
```

The stdio transport reads a dev principal from the environment:

```bash
RHYTHM_DEV_SUBJECT=hana RHYTHM_DEV_SCOPES=rhythm.read,rhythm.write,rhythm.safety make mcp-stdio
```

## Tools

| MCP name | Registry name | Type | Notes |
|---|---|---|---|
| `rhythm_day_state` | `rhythm.day.state` | read | Phase, energy, sleep debt, heat, confidence. Gaps are `null`, never `0` |
| `rhythm_sleep_summary` | `rhythm.sleep.summary` | read | 7-night summary; `confidence: low` below 4 nights |
| `rhythm_plan_preview` | `rhythm.plan.preview` | read | The one primary action + one optional; commits nothing |
| `rhythm_action_library` | `rhythm.action.library` | read | Paginated (`limit`, `offset`, `next_offset`) |
| `rhythm_safety_classify` | `rhythm.safety.classify` | read | Deterministic red/amber/green; **run before acting on symptom text** |
| `rhythm_review_weekly` | `rhythm.review.weekly` | read | Refuses percentages under 3 logged days |
| `rhythm_checkin` | `rhythm.checkin` | write | Morning check-in; unknown fields are omitted, not zeroed |
| `rhythm_commitment_create` | `rhythm.commitment.create` | write | The one commitment of the day |
| `rhythm_commitment_close` | `rhythm.commitment.close` | write, idempotent | Requires `idempotency_key`; replays return the original result |

Every definition carries `title`, a caller-facing `description`, `inputSchema`, and
honest annotations (`readOnlyHint`, `idempotentHint`, …) per the MCP whitepaper.

## Security model

The protocol's known gaps are closed at this layer:

- **Identity on every call.** HTTP requires `Authorization: Bearer <subject:scopes[:agent_id]>`.
  No header → `[unauthenticated]` with a hint. (Dev token format; swap `parse_token`
  for a JWT verifier in production — nothing else changes.)
- **Audience binding.** The token must name this server; tokens cannot be replayed
  across services (confused deputy, part 1).
- **Subject binding.** The token's subject is the only person the call can touch;
  `user_id=other` → `[subject_mismatch]` (part 2).
- **Per-token scopes.** `rhythm.read`, `rhythm.write`, `rhythm.safety` — least
  privilege per tool, not per client.
- **Anti tool-shadowing.** The server's identity is a versioned, hashed manifest at
  `rhythm://manifest` and `/.well-known/rhythm-manifest.json`. Clients pin the digest.
- **No secrets in traces.** Tool arguments are digested; sensitive keys are redacted.

## Transports

- **stdio** — for hosts that spawn the server as a process.
- **streamable HTTP, stateless** — every request is independent, so replicas scale
  horizontally behind a load balancer; `/healthz` + `/readyz` for probes.

## Error channel

Tool failures return `is_error` results whose text is written for the *calling
model*: `[code] message. Next step: hint`. Example:

```
[idempotency_key_required] rhythm.commitment.close requires idempotency_key.
Next step: Supply a caller-generated key so a retry cannot double-apply.
```

## Tests (`mcp/tests/test_mcp.py`)

Real protocol tests against a live streamable-HTTP server: tool listing with
annotations, **schema drift vs the registry**, error guidance channel, unknown tool,
missing token, confused-deputy denial, red-flag routing, the full
checkin → commit → close loop with idempotent replay, resources, prompts,
`/healthz` + manifest endpoints, and token/unit-level security tests.
