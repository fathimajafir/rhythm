# ADR-002 — Clinical staffing model at launch

- **Status:** Accepted
- **Date:** 2026-10-26
- **Deciders:** clinical lead, product lead, commercial lead
- **Related:** design doc §0.3, §9.5, ADR-001

## Context

We committed to "clinician in the loop at launch" (D3). That phrase hides a staffing
decision, and the naive reading — *hire a clinician* — is the wrong shape for a
300-person pilot:

- **The volume is tiny.** At 300 active users, assume 2–4% amber escalations per month
  (6–12 cases) and 0.3–0.5% red (1–2 cases). That is roughly **2–4 clinical cases per week**.
- **The shape is spiky.** Red flags need a human in 15 minutes; amber can wait until
  the same business day. A 0.5 FTE hire is idle 90% of the time and unavailable exactly
  when the SLA bites (in a meeting, on leave, asleep).
- **The deliverables are not just cases.** Protocols, monthly incident review, and audit
  are *governance* work, best done by someone senior for a few hours a month — not by a
  full-time generalist.

The design doc's own estimate (0.3–0.5 FTE per 1,000 active users) works out to
**0.1–0.15 FTE at pilot scale**. Staffing 0.5 FTE would be four times the need.

## Decision

Adopt a **two-part model** for the pilot:

| Role | Who | Commitment | Owns |
|---|---|---|---|
| **Clinical lead** | Named physician (fractional advisor) | 10–15 h/month | Protocols, red/amber definitions, monthly incident review, audit sign-off, escalation adjudication |
| **Coverage partner** | Licensed clinic / telehealth provider on retainer | Contracted SLA | Red-flag response within 15 min during covered hours; amber within the business day; on-call rota |

The **coverage partner holds the SLA**; the **clinical lead holds the standards**.
One person cannot credibly do both at pilot volume — the advisor will be in a meeting
when the page arrives.

Revisit at **~1,000 active users**, or earlier if a renewal requires a named clinical
director on staff.

## Options considered

| Option | Cost (indicative, AED/month) | Time to stand up | Verdict |
|---|---|---|---|
| **A. Employed clinician, 0.5 FTE** | 15,000–22,000 fully loaded | 8–12 weeks (hiring) | Rejected for pilot: slow, idle, and still needs on-call cover for nights |
| **B. Contract clinic only** | 12,000–25,000 retainer + 150–400/case | 3–6 weeks | Workable, but no one owns protocol quality or the audit |
| **C. Advisor + coverage partner** ✅ | 8,000–15,000 advisor + partner retainer | 2–3 weeks | **Chosen**: standards and coverage, at pilot scale |
| D. Protocol-only, no human | ~0 | immediate | Rejected: contradicts D3 and the procurement story |

*Costs are planning placeholders to be replaced by three quotes before signing. They are
not market research.*

## Consequences

**Positive**
- Meets the 15-minute red SLA with a licensed entity that already has 24/7 cover.
- A senior clinician owns protocol quality — the part that actually prevents incidents.
- Procurement gets a licensed counterparty and a named clinical lead, which is what
  security and legal will ask for.
- Total clinical cost at pilot scale is a fraction of a hire.

**Negative / accepted**
- Two contracts, two relationships to manage.
- The advisor is a bottleneck for protocol changes; mitigate with a 3-business-day SLA
  on protocol revisions and a documented deputy.
- Coverage partner staff will not know the product; mitigate with a one-page escalation
  card and a 30-minute briefing per clinician on the rota.

## Non-negotiables (carried from design §9.5)

- Protocols are **signed before launch** and reviewed quarterly.
- Every clinical decision is **logged**: who, what, when, which protocol version.
- Clinicians see **only escalated cases** and only the consented history slice.
- The clinician is a **backstop, not a filter** — no human reads routine messages, or
  both latency and unit economics break.
- If the clinician is unavailable, the system **fails closed**: red pages the on-call
  arrangement; amber queues and the agent says a clinician will follow up tomorrow.

## Review triggers

| Trigger | Action |
|---|---|
| > 20 escalations/month | Re-run the volume model; consider 0.2 FTE employed |
| Any red escalation acked > 15 min | Contract review with the partner; add a second rota slot |
| ~1,000 active users | Move to option A (employed clinical director) |
| Enterprise deal requires named on-staff clinician | Move to option A early |
