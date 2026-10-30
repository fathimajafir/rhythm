# ADR-001 — Pilot employer selection

- **Status:** Accepted (criteria) · Open (which employer)
- **Date:** 2026-10-26
- **Deciders:** product lead, clinical lead, commercial lead
- **Supersedes:** n/a
- **Related:** design doc §12, ADR-002

## Context

We need one employer for a 14-week pilot, and the pilot's statistical design is a
**stepped-wedge across ≥ 4 clusters**. The pilot is also the first time our data-protection
agreement, clinical tier, and residency claims will be examined by someone with lawyers.
Which employer we pick therefore determines the schedule more than any engineering choice.

The tempting criteria (brand recognition, headcount, enthusiasm) are the wrong ranking.
A 10,000-person logo with a 6-month legal review launches later than a 400-person
employer that signs in three weeks.

## Decision

Adopt the following as a **ranked selection spec**, in this order:

| # | Criterion | Threshold |
|---|---|---|
| 1 | Legal team can sign a DPA in weeks | ≤ 6 weeks from first draft to signature |
| 2 | Eligible employees | ≥ 300 |
| 3 | Comparable departments/sites | ≥ 4 clusters, similar role mix and shift pattern |
| 4 | Wellbeing lead with budget authority | can commit pilot spend without a committee |
| 5 | HQ and data residency in-region | storage and processing stay in-country |
| 6 | Language profile recorded | English-dominant or Arabic-dominant — sets the cohort default, not the product |

## Why this order

1. **Legal speed is the schedule.** The DPA is the critical path (§0.1). Everything else
   can be built in parallel; the contract cannot be built at all.
2. **300 is the statistical floor.** Below it, the stepped-wedge cannot distinguish a
   real effect from noise, so the pilot produces an opinion instead of evidence.
3. **4 clusters is the contamination defence.** Employees talk. Individual randomisation
   dies in an open-plan office; cluster assignment is the honest design.
4. **Budget authority avoids the "pilot in name only"** failure — the enthusiastic
   champion with no signature becomes a six-month internal sales cycle.
5. **Residency shortens the security review** and is a hard contractual requirement.
6. **Language is recorded, not assumed.** Defaults are per cohort (§0.4); an
   expat-majority workforce defaults to English, which is fine — as long as it is a
   decision rather than an accident.

## Process (3 weeks, runs parallel to Phase 0)

1. **Longlist (week 1).** 8–12 employers from existing relationships, insurers' group
   clients, and free-zone communities. Do not add inbound logos that fail criterion 1.
2. **Screen (week 1).** Two questions by email:
   - *What did you measure after your last wellness spend?* (If "nothing", they cannot
     defend a renewal, and will not defend ours.)
   - *Who owns the works-council / HR consultation, and when did it last run?*
3. **Score (week 2).** Use `docs/ops/pilot-employer-scorecard.md`. Proceed at ≥ 70/100
   with no criterion at 1.
4. **Legal pre-read (week 2).** Send the DPA summary and security one-pager, not the
   whole pack. Ask for a named reviewer and a date.
5. **Select (week 3).** Sign a letter of intent; start the DPA the same week.

## Consequences

**Positive**
- The pilot starts on the 14-week plan rather than the 26-week one.
- The stepped-wedge is statistically readable, so the adaptivity question gets a real answer.

**Negative / accepted**
- We will pass on larger, better-known employers this cycle.
- Four clusters may mean four sites, which adds travel and onboarding cost.
- A fast legal team is not a proxy for a healthy culture; it only removes one risk.

## What would make us revisit

- No employer meeting criteria 1–3 after 4 weeks of sourcing → widen the geography
  rather than lower the cluster minimum (a noisy pilot is worse than a late one).
- An insurer offers a multi-employer cohort with a pre-signed DPA → that supersedes
  single-employer selection entirely, and is the better deal if it arrives.
