"""Phase 1 core tests: metrics, signals, planner, loop, safety.

These are the tests that must pass before any model is introduced.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from rhythm import safety
from rhythm.domain import (
    CalendarLoad,
    Confidence,
    DayState,
    Locale,
    Phase,
    Profile,
    SignalDay,
    SleepRecord,
)
from rhythm.library import BY_ID
from rhythm.loop import DayLoop, TransitionError
from rhythm.metrics import (
    consistency_rate,
    energy_band,
    is_bad_day,
    max_effort,
    sleep_stats,
)
from rhythm.planner import HistoryView, plan_day, slot_for
from rhythm.signals import RawPoint, build_days, is_heat_day

D = date(2026, 10, 26)


def night(days_ago: int, bed_h: int = 23, wake_h: int = 7, minute: int = 0) -> SleepRecord:
    """Build a night. Bedtimes before noon belong to the wake date (a 3am bedtime)."""
    n = D - timedelta(days=days_ago)
    bed_date = n if bed_h < 12 else n - timedelta(days=1)
    return SleepRecord(
        night_of=n,
        bedtime=datetime.combine(bed_date, time(bed_h, minute)),
        waketime=datetime.combine(n, time(wake_h, minute)),
        source="healthkit",
    )


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
def test_midpoint_handles_midnight():
    r = night(0, bed_h=23, wake_h=7)
    assert r.duration_h == pytest.approx(8.0)
    assert r.midpoint_min == 180  # 03:00


def test_no_records_is_unknown_not_zero():
    stats = sleep_stats([])
    assert stats.nights == 0
    assert stats.duration_h is None
    assert stats.debt_h is None
    assert stats.confidence is Confidence.UNKNOWN


def test_min_n_gate_blocks_trend_claims():
    stats = sleep_stats([night(1), night(2)], target_h=7.5)
    assert stats.confidence is Confidence.LOW
    assert stats.regularity_sd_min is None


def test_regularity_uses_circular_distance():
    # 23:50 and 00:10 are 20 minutes apart, not 23 hours.
    a = night(3, bed_h=23, wake_h=7, minute=40)
    b = night(2, bed_h=23, wake_h=7, minute=59)
    c = night(1, bed_h=0, wake_h=8, minute=0)
    d = night(0, bed_h=0, wake_h=8, minute=20)
    stats = sleep_stats([a, b, c, d])
    assert stats.regularity_sd_min is not None
    assert stats.regularity_sd_min < 60


def test_debt_caps_per_night():
    records = [night(i, bed_h=3, wake_h=5) for i in range(7)]  # 2h each
    stats = sleep_stats(records, target_h=7.5)
    # 5.5h shortfall/night, capped at 3h, 7 nights, capped at 10h total
    assert stats.debt_h == 10.0


def test_energy_self_report_wins():
    assert energy_band(reported=5, sleep_h=3.0, debt_h=9.0) == 5
    assert energy_band(reported=None, sleep_h=4.0, debt_h=8.0) == 1
    assert energy_band(reported=None, sleep_h=None, debt_h=None) is None


def test_unknown_energy_stays_conservative():
    assert max_effort(None, None, None) == 2
    assert max_effort(1, None, None) == 1
    assert max_effort(5, 8.0, 0.0) == 3


def test_bad_day_thresholds():
    assert is_bad_day(sleep_h=4.0, energy=3, debt_h=0)
    assert is_bad_day(sleep_h=8.0, energy=1, debt_h=0)
    assert is_bad_day(sleep_h=8.0, energy=4, debt_h=6)
    assert not is_bad_day(sleep_h=7.5, energy=4, debt_h=1)


def test_consistency_counts_attempts_not_perfection():
    assert consistency_rate(["done", "miss", "partial", "done"]) == 0.75
    assert consistency_rate([]) == 0.0


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------
def test_source_priority_native_beats_aggregator():
    points = [
        RawPoint(day=D, kind="steps", source="fitbit", value=1200),
        RawPoint(day=D, kind="steps", source="healthkit", value=8400),
    ]
    days = build_days(points)
    assert days[D].steps == 8400


def test_missing_signal_stays_none():
    days = build_days([RawPoint(day=D, kind="steps", source="garmin", value=None)])
    assert days[D].steps is None


def test_heat_only_when_known():
    assert is_heat_day(42.0) is True
    assert is_heat_day(38.0) is False
    assert is_heat_day(None) is False


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------
def state(**kw) -> DayState:
    base = dict(
        user_id="u1",
        day=D,
        phase=Phase.CHECKED_IN,
        locale=Locale.EN,
        energy=4,
        sleep_h=7.5,
        sleep_debt_h=0.5,
        confidence=Confidence.HIGH,
    )
    base.update(kw)
    return DayState(**base)  # type: ignore[arg-type]


PROFILE = Profile(user_id="u1")


def test_slot_mapping():
    assert slot_for(8) is not None and slot_for(8).value == "morning"
    assert slot_for(13).value == "midday"
    assert slot_for(21).value == "evening"


def test_heat_excludes_outdoor_actions():
    midday = datetime.combine(D, time(13, 0))
    plan = plan_day(state=state(heat=True), profile=PROFILE, at=midday)
    assert plan.primary is not None
    assert plan.primary.outdoor is False


def test_low_energy_caps_effort():
    midday = datetime.combine(D, time(13, 0))
    plan = plan_day(state=state(energy=1), profile=PROFILE, at=midday)
    assert plan.primary is not None
    assert plan.primary.effort == 1


def test_planner_is_deterministic():
    midday = datetime.combine(D, time(13, 0))
    a = plan_day(state=state(), profile=PROFILE, at=midday)
    b = plan_day(state=state(), profile=PROFILE, at=midday)
    assert a.primary.id == b.primary.id


def test_bad_day_downgrades_duration():
    evening = datetime.combine(D, time(21, 0))
    plan = plan_day(state=state(bad_day=True, energy=1), profile=PROFILE, at=evening)
    assert plan.downgraded is True
    assert plan.primary is not None
    assert plan.primary.duration_min <= plan.primary.fallback_min


def test_constraints_remove_tagged_actions():
    evening = datetime.combine(D, time(20, 0))
    injured = Profile(user_id="u1", constraints=frozenset({"knee"}))
    plan = plan_day(state=state(energy=5), profile=injured, at=evening)
    assert plan.primary is not None
    assert "knee" not in BY_ID[plan.primary.id].tags


def test_history_biases_toward_what_worked():
    midday = datetime.combine(D, time(13, 0))
    history = HistoryView(success_rate={"m_walk_call": 1.0}, yesterday_action_id=None)
    plan = plan_day(state=state(), profile=PROFILE, at=midday, history=history)
    assert plan.primary is not None
    assert plan.primary.behaviour.value == "movement"


def test_no_eligible_action_returns_no_plan():
    # 03:00 with heat: nothing matches the morning slot outdoors-only? use a narrow library
    early = datetime.combine(D, time(4, 0))
    plan = plan_day(state=state(), profile=PROFILE, at=early, library=())
    assert plan.primary is None


# --------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------
def build_loop() -> DayLoop:
    loop = DayLoop()
    loop.register(Profile(user_id="u1"))
    return loop


def test_full_day_loop():
    loop = build_loop()
    signals = SignalDay(day=D, steps=3000, active_minutes=10, temp_c=36.0)
    loop.check_in("u1", D, datetime.combine(D, time(8, 0)), energy=3, signals=signals)

    commitment = loop.commit("u1", D, datetime.combine(D, time(9, 0)))
    assert commitment.status == "open"

    nudge = loop.nudge("u1", D)
    assert nudge is not None

    closed = loop.close("u1", commitment.id, "done")
    assert closed.status == "done"
    assert loop.state("u1", D).phase is Phase.CLOSED


def test_cannot_commit_before_checkin():
    loop = build_loop()
    with pytest.raises(TransitionError):
        loop.commit("u1", D, datetime.combine(D, time(9, 0)))


def test_close_is_idempotent():
    loop = build_loop()
    loop.check_in("u1", D, datetime.combine(D, time(8, 0)), energy=4)
    c = loop.commit("u1", D, datetime.combine(D, time(9, 0)))

    first = loop.close("u1", c.id, "done", idempotency_key="k1")
    second = loop.close("u1", c.id, "miss", idempotency_key="k1")
    assert first.status == second.status == "done"  # replay returns the original


def test_close_rejects_other_users_commitment():
    loop = build_loop()
    loop.register(Profile(user_id="u2"))
    loop.check_in("u1", D, datetime.combine(D, time(8, 0)), energy=4)
    c = loop.commit("u1", D, datetime.combine(D, time(9, 0)))
    with pytest.raises(PermissionError):
        loop.close("u2", c.id, "done")


def test_one_commitment_per_day():
    loop = build_loop()
    loop.check_in("u1", D, datetime.combine(D, time(8, 0)), energy=4)
    a = loop.commit("u1", D, datetime.combine(D, time(9, 0)))
    b = loop.commit("u1", D, datetime.combine(D, time(11, 0)))
    assert a.id == b.id


def test_miss_carries_no_penalty_and_no_state_break():
    loop = build_loop()
    loop.check_in("u1", D, datetime.combine(D, time(8, 0)), energy=4)
    c = loop.commit("u1", D, datetime.combine(D, time(9, 0)))
    closed = loop.close("u1", c.id, "miss")
    assert closed.status == "miss"

    # Next day still plans normally.
    nxt = D + timedelta(days=1)
    loop.check_in("u1", nxt, datetime.combine(nxt, time(8, 0)), energy=3)
    plan = loop.plan("u1", nxt, datetime.combine(nxt, time(9, 0)))
    assert plan.primary is not None


def test_red_flag_opens_escalation_and_blocks_plan():
    loop = build_loop()
    verdict = safety.classify("I have chest pain right now")
    assert verdict.band is safety.SafetyBand.RED
    assert verdict.blocks_plan is True

    state = loop.check_in("u1", D, datetime.combine(D, time(8, 0)), energy=2, verdict=verdict)
    assert state.bad_day is True
    assert len(loop.store.escalations) == 1


def test_weekly_review_is_deterministic_text():
    loop = build_loop()
    text = loop.weekly_review("u1", D)
    assert isinstance(text, str) and text
    assert "too early for a trend" in text  # minimum-n guard, not a fake 0% or 100%
    assert "Next week" in text
