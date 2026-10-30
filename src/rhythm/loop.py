"""The per-user day state machine (design doc §4.1).

IDLE -> CHECKED_IN -> COMMITTED -> NUDGED -> CLOSED -> REVIEWED

A coach shows up; a chat waits to be spoken to. That is why this is a state
machine and not a conversation loop — and why cost is bounded per user per day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from . import coach
from .domain import (
    Commitment,
    Confidence,
    DayState,
    Locale,
    Phase,
    Plan,
    Profile,
    SignalDay,
)
from .metrics import energy_band, is_bad_day, sleep_stats
from .library import BY_ID
from .planner import HistoryView, in_window, plan_day
from .safety import SafetyBand, Verdict
from .signals import is_heat_day
from .store import Store

OPEN_STATUSES = ("open",)


class TransitionError(RuntimeError):
    """Raised when the loop is asked to do something out of order."""


class DayLoop:
    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or Store()
        self._seq = 0

    # -- setup -------------------------------------------------------------
    def register(self, profile: Profile) -> Profile:
        return self.store.put_profile(profile)

    # -- day state ---------------------------------------------------------
    def state(self, user_id: str, day: date) -> DayState:
        existing = self.store.get_day(user_id, day)
        if existing:
            return existing
        profile = self.store.profile(user_id)
        return DayState(
            user_id=user_id,
            day=day,
            phase=Phase.IDLE,
            locale=profile.locale,
        )

    def check_in(
        self,
        user_id: str,
        day: date,
        at: datetime,
        *,
        energy: Optional[int] = None,
        signals: Optional[SignalDay] = None,
        verdict: Optional[Verdict] = None,
    ) -> DayState:
        """Fold the morning check-in into state. A red verdict blocks the plan."""
        profile = self.store.profile(user_id)
        current = self.state(user_id, day)

        if signals is not None:
            self.store.put_signals(user_id, signals)

        records = self.store.sleep_history.get(user_id, [])
        stats = sleep_stats(records, target_h=profile.sleep_target_h)
        band = energy_band(reported=energy, sleep_h=stats.duration_h, debt_h=stats.debt_h)

        day_signals = self.store.day_signals(user_id, day)
        temp = day_signals.temp_c if day_signals else None
        dense = bool(day_signals and day_signals.calendar.dense)

        state = DayState(
            user_id=user_id,
            day=day,
            phase=Phase.CHECKED_IN,
            locale=profile.locale,
            energy=band,
            sleep_h=stats.duration_h,
            sleep_midpoint_min=stats.midpoint_min,
            sleep_debt_h=stats.debt_h,
            regularity_sd_min=stats.regularity_sd_min,
            active_minutes=day_signals.active_minutes if day_signals else None,
            heat=is_heat_day(temp),
            dense_calendar=dense,
            confidence=stats.confidence,
            bad_day=is_bad_day(
                sleep_h=stats.duration_h, energy=band, debt_h=stats.debt_h
            ),
        )

        if verdict is not None and verdict.band is SafetyBand.RED:
            state.bad_day = True
            self.store.log_escalation(
                {
                    "user_id": user_id,
                    "day": day.isoformat(),
                    "band": verdict.band.value,
                    "matched": list(verdict.matched),
                    "sla": "red: 15 min",
                }
            )

        self.store.put_day(state)
        return state

    # -- planning ----------------------------------------------------------
    def plan(self, user_id: str, day: date, at: datetime, *, reason: str = "") -> Plan:
        state = self.state(user_id, day)
        if state.phase is Phase.IDLE:
            raise TransitionError("check in before planning")

        profile = self.store.profile(user_id)
        history = self._history(user_id, day)
        plan = plan_day(state=state, profile=profile, at=at, history=history)

        if reason:
            plan = Plan(
                primary=plan.primary,
                optional=plan.optional,
                reason=reason,
                downgraded=plan.downgraded,
            )
        return plan

    def commit(self, user_id: str, day: date, at: datetime) -> Commitment:
        """Exactly one commitment. This is the product."""
        state = self.state(user_id, day)
        if state.phase is Phase.IDLE:
            raise TransitionError("check in before committing")

        existing = [
            c
            for c in self.store.commitments_between(user_id, day, day)
            if c.status in OPEN_STATUSES
        ]
        if existing:
            return existing[0]

        plan = self.plan(user_id, day, at)
        if plan.primary is None:
            raise TransitionError("no eligible action to commit to")

        self._seq += 1
        commitment = Commitment(
            id=f"c_{user_id}_{day.isoformat()}_{self._seq}",
            user_id=user_id,
            day=day,
            action_id=plan.primary.id,
            title=plan.primary.title_ar
            if state.locale is Locale.AR
            else plan.primary.title_en,
            duration_min=plan.primary.duration_min,
            slot=plan.primary.slot,
            fallback_offered=plan.downgraded,
        )
        self.store.put_commitment(commitment)
        state.phase = Phase.COMMITTED
        self.store.put_day(state)
        return commitment

    def nudge(
        self,
        user_id: str,
        day: date,
        at: Optional[datetime] = None,
        minutes_left: int = 5,
    ) -> Optional[str]:
        state = self.state(user_id, day)
        if state.phase is not Phase.COMMITTED:
            return None
        open_items = [c for c in self.store.commitments_between(user_id, day, day) if c.status == "open"]
        if not open_items:
            return None

        # Never nudge an ask whose window has passed — that is how an app
        # becomes noise instead of a coach.
        action = BY_ID.get(open_items[0].action_id)
        if at is not None and action is not None and not in_window(action, at.hour):
            return None

        state.phase = Phase.NUDGED
        self.store.put_day(state)
        return coach.nudge(state.locale, open_items[0], minutes_left)

    # -- closing -----------------------------------------------------------
    def close(
        self,
        user_id: str,
        commitment_id: str,
        status: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Commitment:
        if status not in ("done", "partial", "miss"):
            raise ValueError("status must be done | partial | miss")

        if idempotency_key:
            prior = self.store.close_keys.get(idempotency_key)
            if prior is not None:
                return self.store.get_commitment(prior)

        from dataclasses import replace

        commitment = self.store.get_commitment(commitment_id)
        if commitment.user_id != user_id:
            raise PermissionError("commitment belongs to another user")
        if commitment.status != "open":
            return commitment

        closed = replace(commitment, status=status)
        self.store.put_commitment(closed)

        state = self.store.get_day(user_id, commitment.day)
        if state is not None:
            state.phase = Phase.CLOSED
            self.store.put_day(state)

        if idempotency_key:
            self.store.put_close_key(idempotency_key, commitment_id)

        return closed

    # -- review ------------------------------------------------------------
    def weekly_review(self, user_id: str, day: date) -> str:
        profile = self.store.profile(user_id)
        state = self.state(user_id, day)
        stats = sleep_stats(
            self.store.sleep_history.get(user_id, []), target_h=profile.sleep_target_h
        )
        commitments = self.store.commitments_between(
            user_id, day - timedelta(days=6), day
        )
        statuses = [c.status for c in commitments if c.status != "open"]

        next_plan = plan_day(
            state=state,
            profile=profile,
            at=datetime.combine(day, datetime.min.time()).replace(hour=8),
            history=self._history(user_id, day),
        )
        next_action = (
            coach.behaviour_label(state.locale, next_plan.primary.behaviour)
            if next_plan.primary
            else None
        )

        return coach.weekly_review(
            state.locale,
            state=state,
            stats=stats,
            statuses=statuses,
            next_action=next_action,
        )

    # -- helpers -----------------------------------------------------------
    def _history(self, user_id: str, day: date) -> HistoryView:
        past = self.store.commitments_between(user_id, day - timedelta(days=28), day - timedelta(days=1))
        totals: dict[str, list[str]] = {}
        for c in past:
            totals.setdefault(c.action_id, []).append(c.status)
        success = {
            action_id: (sum(1 for s in statuses if s in ("done", "partial")) / len(statuses))
            for action_id, statuses in totals.items()
        }
        yesterday = self.store.commitments_between(user_id, day - timedelta(days=1), day - timedelta(days=1))
        return HistoryView(
            success_rate=success,
            yesterday_action_id=yesterday[0].action_id if yesterday else None,
        )
