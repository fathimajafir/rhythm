"""The planner: a constrained optimiser, not a language model.

Contract (design doc §4.2): emits exactly one primary action plus one optional.
Eligibility is hard; ranking is soft; the result is reproducible for the same
state — which is what makes it auditable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from .domain import Action, Behaviour, DayState, Plan, Profile, Slot
from .library import LIBRARY
from .metrics import max_effort


@dataclass(frozen=True)
class HistoryView:
    """Procedural memory: what actually worked for this person."""

    success_rate: dict[str, float]  # action_id -> 0..1
    yesterday_action_id: Optional[str] = None


def slot_for(hour: int) -> Slot:
    if hour < 12:
        return Slot.MORNING
    if hour < 17:
        return Slot.MIDDAY
    return Slot.EVENING


SLOT_WINDOW: dict[Slot, tuple[int, int]] = {
    Slot.MORNING: (6, 11),
    Slot.MIDDAY: (11, 17),
    Slot.EVENING: (17, 23),
}


def window_for(action: Action) -> tuple[int, int]:
    return action.window or SLOT_WINDOW[action.slot]


def in_window(action: Action, hour: int) -> bool:
    start, end = window_for(action)
    return start <= hour <= end


def eligible(
    action: Action,
    *,
    state: DayState,
    profile: Profile,
    slot: Slot,
    effort_ceiling: int,
    hour: int,
) -> bool:
    if action.slot != slot:
        return False
    if not in_window(action, hour):
        return False
    if action.effort > effort_ceiling:
        return False
    if action.outdoor and state.heat:
        return False
    if state.energy is not None and state.energy < action.min_energy:
        return False
    if action.tags & profile.constraints:
        return False
    return True


def _priority(state: DayState) -> dict[Behaviour, float]:
    """Which behaviour earns the one slot today, from the signals alone."""
    weights = {Behaviour.SLEEP: 1.0, Behaviour.MOVEMENT: 1.0, Behaviour.WINDDOWN: 1.0}

    if state.sleep_debt_h is not None and state.sleep_debt_h >= 3:
        weights[Behaviour.SLEEP] += 2.0
        weights[Behaviour.WINDDOWN] += 1.0
    if state.regularity_sd_min is not None and state.regularity_sd_min > 60:
        weights[Behaviour.SLEEP] += 1.5
    if state.active_minutes is not None and state.active_minutes < 15:
        weights[Behaviour.MOVEMENT] += 1.5
    if state.confidence.value == "unknown":
        weights[Behaviour.MOVEMENT] += 0.5  # low-friction, self-evident win
    if state.dense_calendar:
        weights[Behaviour.MOVEMENT] -= 0.5
        weights[Behaviour.WINDDOWN] += 1.0
    return weights


def plan_day(
    *,
    state: DayState,
    profile: Profile,
    at: datetime,
    history: Optional[HistoryView] = None,
    library: Sequence[Action] = LIBRARY,
) -> Plan:
    slot = slot_for(at.hour)
    ceiling = max_effort(state.energy, state.sleep_h, state.sleep_debt_h)
    weights = _priority(state)
    history = history or HistoryView(success_rate={})

    candidates = [
        a
        for a in library
        if eligible(
            a,
            state=state,
            profile=profile,
            slot=slot,
            effort_ceiling=ceiling,
            hour=at.hour,
        )
    ]

    if not candidates:
        # Never leave the user with a menu, and never invent an action.
        return Plan(primary=None, optional=None, reason="no eligible action for this slot")

    scored = sorted(candidates, key=lambda a: (_score(a, weights, state, history), a.id), reverse=True)
    primary = scored[0]

    optional = next(
        (a for a in scored[1:] if a.behaviour != primary.behaviour),
        None,
    )

    downgraded = state.bad_day or primary.effort > ceiling
    duration = primary.fallback_min if downgraded else primary.duration_min
    primary = _with_duration(primary, duration)

    return Plan(
        primary=primary,
        optional=optional,
        reason=_reason(primary, state, downgraded),
        downgraded=downgraded,
    )


def _score(
    action: Action,
    weights: dict[Behaviour, float],
    state: DayState,
    history: HistoryView,
) -> float:
    score = weights[action.behaviour] * 2.0
    score += 1.0 - (action.effort - 1) * 0.5  # friction penalty
    score += history.success_rate.get(action.id, 0.0) * 1.5
    if action.id == history.yesterday_action_id:
        score -= 0.7  # novelty budget: don't repeat the same ask
    if state.dense_calendar and action.duration_min > 10:
        score -= 0.5
    return score


def _with_duration(action: Action, duration: int) -> Action:
    from dataclasses import replace

    return replace(action, duration_min=duration)


def _reason(action: Action, state: DayState, downgraded: bool) -> str:
    if downgraded:
        return "bad-day mode: the bar is deliberately low"
    if action.behaviour == Behaviour.SLEEP:
        return "sleep is the bottleneck today"
    if action.behaviour == Behaviour.MOVEMENT:
        return "movement fits the gap in the day"
    return "wind-down protects tonight's sleep"
