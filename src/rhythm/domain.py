"""Domain model for Rhythm.

Pure stdlib: no framework, no model calls. Phase 1 must run with no LLM at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    IDLE = "idle"
    CHECKED_IN = "checked_in"
    COMMITTED = "committed"
    NUDGED = "nudged"
    CLOSED = "closed"
    REVIEWED = "reviewed"


class Behaviour(str, Enum):
    SLEEP = "sleep"
    MOVEMENT = "movement"
    WINDDOWN = "winddown"


class Slot(str, Enum):
    MORNING = "morning"
    MIDDAY = "midday"
    EVENING = "evening"


class Locale(str, Enum):
    EN = "en"
    AR = "ar"


class Confidence(str, Enum):
    HIGH = "high"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Profile:
    """Semantic memory: only what the user told us, never inferred."""

    user_id: str
    locale: Locale = Locale.EN
    wake_target: time = time(7, 0)
    sleep_target_h: float = 7.5
    constraints: frozenset[str] = frozenset()
    movement_target_min_week: int = 90


@dataclass(frozen=True)
class Action:
    id: str
    behaviour: Behaviour
    slot: Slot
    title_en: str
    title_ar: str
    duration_min: int
    effort: int  # 1 (tiny) .. 3 (hard) — never above what the day can carry
    fallback_min: int  # the bad-day version
    outdoor: bool = False
    min_energy: int = 1  # 1..5
    anchor_en: str = ""
    anchor_ar: str = ""
    tags: frozenset[str] = frozenset()
    # Hours during which the ask still makes sense. "Get up at your usual time"
    # at 09:05 is nonsense; a slot alone is too coarse to catch that.
    window: Optional[tuple[int, int]] = None


@dataclass(frozen=True)
class SleepRecord:
    night_of: date  # the morning you woke
    bedtime: datetime
    waketime: datetime
    source: str
    confidence: float = 1.0

    @property
    def duration_h(self) -> float:
        delta = self.waketime - self.bedtime
        if delta.total_seconds() <= 0:  # crossed midnight
            delta += timedelta(days=1)
        return delta.total_seconds() / 3600.0

    @property
    def midpoint_min(self) -> int:
        """Wall-clock midpoint in minutes since midnight (0..1439)."""
        start = self.bedtime.hour * 60 + self.bedtime.minute
        return int((start + self.duration_h * 30) % 1440)


@dataclass(frozen=True)
class SleepStats:
    nights: int
    duration_h: Optional[float]
    midpoint_min: Optional[int]
    debt_h: Optional[float]
    regularity_sd_min: Optional[float]
    confidence: Confidence


@dataclass(frozen=True)
class CalendarLoad:
    events: int = 0
    first_start: Optional[time] = None
    last_end: Optional[time] = None

    @property
    def dense(self) -> bool:
        return self.events >= 6


@dataclass
class SignalDay:
    """Canonical signal schema. `None` means unknown — never zero."""

    day: date
    sleep: Optional[SleepRecord] = None
    steps: Optional[int] = None
    active_minutes: Optional[int] = None
    hrv_ms: Optional[float] = None
    resting_hr: Optional[int] = None
    calendar: CalendarLoad = field(default_factory=CalendarLoad)
    temp_c: Optional[float] = None


@dataclass
class DayState:
    user_id: str
    day: date
    phase: Phase
    locale: Locale
    energy: Optional[int] = None  # 1..5, self-reported only
    sleep_h: Optional[float] = None
    sleep_midpoint_min: Optional[int] = None
    sleep_debt_h: Optional[float] = None
    regularity_sd_min: Optional[float] = None
    active_minutes: Optional[int] = None
    heat: bool = False
    dense_calendar: bool = False
    confidence: Confidence = Confidence.UNKNOWN
    bad_day: bool = False


@dataclass(frozen=True)
class Commitment:
    id: str
    user_id: str
    day: date
    action_id: str
    title: str
    duration_min: int
    slot: Slot
    status: str = "open"  # open | done | partial | miss
    fallback_offered: bool = False


@dataclass(frozen=True)
class Plan:
    primary: Optional[Action]
    optional: Optional[Action]
    reason: str
    downgraded: bool = False
