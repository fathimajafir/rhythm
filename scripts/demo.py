"""A full day loop in the terminal. No model, no server, no database.

Run: PYTHONPATH=src python scripts/demo.py
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from rhythm import coach
from rhythm.domain import CalendarLoad, Locale, Profile, SignalDay, SleepRecord
from rhythm.loop import DayLoop

DAY = date(2026, 10, 26)


def night(n: date, bed_h: int, wake_h: int) -> SleepRecord:
    bed_date = n - timedelta(days=1) if bed_h >= 12 else n
    return SleepRecord(
        night_of=n,
        bedtime=datetime.combine(bed_date, time(bed_h, 0)),
        waketime=datetime.combine(n, time(wake_h, 0)),
        source="healthkit",
    )


def rule(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def main() -> None:
    loop = DayLoop()
    profile = Profile(user_id="hana", locale=Locale.EN)
    loop.register(profile)

    rule("Four rough nights: the signal, not the guess")
    for i, (bed, wake) in enumerate([(1, 6), (1, 6), (2, 7), (1, 5)], start=1):
        loop.store.add_sleep("hana", night(DAY - timedelta(days=i), bed, wake))

    signals = SignalDay(
        day=DAY,
        steps=1800,
        active_minutes=6,
        temp_c=41.0,
        calendar=CalendarLoad(events=7),
    )

    rule("08:00 — check-in")
    state = loop.check_in("hana", DAY, datetime.combine(DAY, time(8, 0)), energy=2, signals=signals)
    print(coach.checkin_prompt(state.locale))
    print(
        f"state: energy={state.energy} sleep={state.sleep_h}h debt={state.sleep_debt_h}h "
        f"regularity={state.regularity_sd_min} heat={state.heat} bad_day={state.bad_day} "
        f"confidence={state.confidence.value}"
    )

    rule("09:00 — plan (one thing, deliberately small)")
    plan = loop.plan("hana", DAY, datetime.combine(DAY, time(9, 0)))
    print("reason:", plan.reason, "| downgraded:", plan.downgraded)
    print(coach.proposal(state.locale, plan))

    rule("09:05 — commit")
    commitment = loop.commit("hana", DAY, datetime.combine(DAY, time(9, 5)))
    print(f"committed: {commitment.title} ({commitment.duration_min} min, {commitment.slot.value})")

    rule("10:00 — nudge (inside the action's window)")
    print(loop.nudge("hana", DAY, at=datetime.combine(DAY, time(10, 0))))

    rule("21:30 — close")
    closed = loop.close("hana", commitment.id, "partial", idempotency_key="demo-1")
    print(coach.close_ack(state.locale, closed.status))

    rule("Sunday — weekly review")
    print(loop.weekly_review("hana", DAY))

    rule("Safety router (deterministic, outside the model)")
    from rhythm import safety

    for text in ("I have chest pain", "heart racing after coffee", "slept fine, energy 4"):
        v = safety.classify(text)
        print(f"{v.band.value:6} | {text:28} | escalation={v.opens_escalation} block={v.blocks_plan}")

    print("\nZero model calls. That is the point of Phase 1.\n")


if __name__ == "__main__":
    main()
