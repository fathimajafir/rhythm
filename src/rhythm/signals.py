"""Signal normalisation for the hybrid wearable path (design doc §4.4).

Two ingest routes (HealthKit deep + aggregator breadth) converge here.
The rule that prevents most wearable bugs: a gap is unknown, never zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from .domain import CalendarLoad, SignalDay, SleepRecord

# Native path outranks the aggregator; self-report is the floor.
SOURCE_PRIORITY: tuple[str, ...] = (
    "healthkit",
    "health_connect",
    "oura",
    "garmin",
    "fitbit",
    "samsung",
    "self_report",
)

HEAT_THRESHOLD_C = 40.0


@dataclass(frozen=True)
class RawPoint:
    day: date
    kind: str  # sleep | steps | active_minutes | hrv | resting_hr | temp
    source: str
    value: Optional[float] = None
    bedtime: Optional[datetime] = None
    waketime: Optional[datetime] = None
    confidence: float = 1.0


def _rank(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def build_days(
    points: Iterable[RawPoint],
    *,
    calendar: Optional[dict[date, CalendarLoad]] = None,
) -> dict[date, SignalDay]:
    """Fold raw vendor points into canonical days, resolving conflicts by source."""
    grouped: dict[date, dict[str, list[RawPoint]]] = {}
    for p in points:
        grouped.setdefault(p.day, {}).setdefault(p.kind, []).append(p)

    days: dict[date, SignalDay] = {}
    for day, kinds in grouped.items():
        day_signals = SignalDay(day=day)

        sleep_candidates = [
            p
            for p in kinds.get("sleep", [])
            if p.bedtime is not None and p.waketime is not None
        ]
        if sleep_candidates:
            best = min(sleep_candidates, key=lambda p: (_rank(p.source), -p.confidence))
            day_signals.sleep = SleepRecord(
                night_of=day,
                bedtime=best.bedtime,  # type: ignore[arg-type]
                waketime=best.waketime,  # type: ignore[arg-type]
                source=best.source,
                confidence=best.confidence,
            )

        for kind, attr in (
            ("steps", "steps"),
            ("active_minutes", "active_minutes"),
            ("hrv", "hrv_ms"),
            ("resting_hr", "resting_hr"),
            ("temp", "temp_c"),
        ):
            values = kinds.get(kind, [])
            if values:
                best = min(values, key=lambda p: (_rank(p.source), -p.confidence))
                if best.value is not None:  # explicit None stays unknown
                    setattr(day_signals, attr, _coerce(kind, best.value))

        if calendar and day in calendar:
            day_signals.calendar = calendar[day]

        days[day] = day_signals
    return days


def _coerce(kind: str, value: float):
    if kind in ("steps", "active_minutes", "resting_hr"):
        return int(round(value))
    return float(round(value, 2))


def is_heat_day(temp_c: Optional[float], threshold_c: float = HEAT_THRESHOLD_C) -> bool:
    """Unknown temperature is not a heat day — but it is also not a licence to plan outdoors."""
    return temp_c is not None and temp_c >= threshold_c


def merge(target: SignalDay, incoming: SignalDay) -> SignalDay:
    """Later-configured source wins only where the target is unknown."""
    if target.day != incoming.day:
        raise ValueError("cannot merge different days")
    return SignalDay(
        day=target.day,
        sleep=target.sleep or incoming.sleep,
        steps=target.steps if target.steps is not None else incoming.steps,
        active_minutes=(
            target.active_minutes
            if target.active_minutes is not None
            else incoming.active_minutes
        ),
        hrv_ms=target.hrv_ms if target.hrv_ms is not None else incoming.hrv_ms,
        resting_hr=(
            target.resting_hr if target.resting_hr is not None else incoming.resting_hr
        ),
        calendar=target.calendar,
        temp_c=target.temp_c if target.temp_c is not None else incoming.temp_c,
    )
