"""In-memory store. Phase 1 only — the design calls for separate databases for
the individual store and the employer aggregate (design doc §11.1). This module
is the individual store, and nothing here is reachable from the reporting layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .domain import Commitment, DayState, Profile, SignalDay, SleepRecord


@dataclass
class Store:
    profiles: dict[str, Profile] = field(default_factory=dict)
    days: dict[tuple[str, date], DayState] = field(default_factory=dict)
    commitments: dict[str, Commitment] = field(default_factory=dict)
    signals: dict[tuple[str, date], SignalDay] = field(default_factory=dict)
    sleep_history: dict[str, list[SleepRecord]] = field(default_factory=dict)
    close_keys: dict[str, str] = field(default_factory=dict)
    escalations: list[dict] = field(default_factory=list)

    # -- profiles ----------------------------------------------------------
    def put_profile(self, profile: Profile) -> Profile:
        self.profiles[profile.user_id] = profile
        return profile

    def profile(self, user_id: str) -> Profile:
        if user_id not in self.profiles:
            raise KeyError(f"unknown user: {user_id}")
        return self.profiles[user_id]

    # -- signals -----------------------------------------------------------
    def put_signals(self, user_id: str, day: SignalDay) -> None:
        self.signals[(user_id, day.day)] = day
        if day.sleep is not None:
            self.add_sleep(user_id, day.sleep)

    def add_sleep(self, user_id: str, record: SleepRecord) -> None:
        history = self.sleep_history.setdefault(user_id, [])
        # Dedupe by night: a re-sync replaces the earlier record.
        history[:] = [r for r in history if r.night_of != record.night_of]
        history.append(record)
        history.sort(key=lambda r: r.night_of)

    def day_signals(self, user_id: str, day: date) -> Optional[SignalDay]:
        return self.signals.get((user_id, day))

    # -- state -------------------------------------------------------------
    def put_day(self, state: DayState) -> DayState:
        self.days[(state.user_id, state.day)] = state
        return state

    def get_day(self, user_id: str, day: date) -> Optional[DayState]:
        return self.days.get((user_id, day))

    # -- commitments -------------------------------------------------------
    def put_commitment(self, commitment: Commitment) -> Commitment:
        self.commitments[commitment.id] = commitment
        return commitment

    def get_commitment(self, commitment_id: str) -> Commitment:
        if commitment_id not in self.commitments:
            raise KeyError(f"unknown commitment: {commitment_id}")
        return self.commitments[commitment_id]

    def commitments_between(self, user_id: str, start: date, end: date) -> list[Commitment]:
        return sorted(
            (
                c
                for c in self.commitments.values()
                if c.user_id == user_id and start <= c.day <= end
            ),
            key=lambda c: c.day,
        )

    def put_close_key(self, key: str, commitment_id: str) -> None:
        self.close_keys[key] = commitment_id

    # -- escalations -------------------------------------------------------
    def log_escalation(self, payload: dict) -> dict:
        self.escalations.append(payload)
        return payload
