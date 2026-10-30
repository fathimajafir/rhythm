# Decision log

> Note: the personal memory bank is not connected in this environment (OmniWork
> extensions unavailable), so the locked decisions live here as the durable project
> record. This file is a project decision log, not private memory — safe to commit.

## One-line summary (v0.3)

**Rhythm is B2B2C employer-paid behind a structural data wall, with hybrid wearables (Apple HealthKit deep + aggregator), a clinician-in-the-loop safety tier with tiered SLA, English-first coaching with Arabic authored in parallel and per-cohort locale defaults, targeting a 14-week stepped-wedge pilot of ≥300 employees across ≥4 departments at an employer whose legal team can sign a DPA in weeks.**

## Locked decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Go-to-market | B2B2C — employer/insurer pays; employer sees k-anonymised aggregates only |
| D2 | Wearables | Hybrid: Apple HealthKit (source of truth) + aggregator (breadth, activation-gated) |
| D3 | Clinical | Clinician in the loop at launch — backstop, not filter; see ADR-002 |
| D4 | Language | English-first, Arabic authored in parallel, defaults per cohort |
| — | Pilot | Stepped-wedge, ≥ 300 employees, ≥ 4 clusters; see ADR-001 |

## Still open

1. **Select the pilot employer** against ADR-001 — start the DPA the same week.
2. **Engage the clinical lead + coverage partner** per ADR-002 (2–3 weeks lead time).
