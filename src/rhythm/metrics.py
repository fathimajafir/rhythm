"""Deterministic metrics. All arithmetic lives here, never in a model.

Design rule from the design doc (§3): "All numbers are computed in Python."
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from .domain import Confidence, SleepRecord, SleepStats

MIN_NIGHTS_FOR_TREND = 4
DEBT_WINDOW_NIGHTS = 7
DEBT_CAP_H = 10.0
MAX_SHORTFALL_PER_NIGHT = 3.0


def sleep_stats(records: Sequence[SleepRecord], target_h: float = 7.5) -> SleepStats:
    """Compute last-night values plus a 7-night debt and regularity.

    A gap is unknown, not zero: with no records every field is None.
    """
    if not records:
        return SleepStats(
            nights=0,
            duration_h=None,
            midpoint_min=None,
            debt_h=None,
            regularity_sd_min=None,
            confidence=Confidence.UNKNOWN,
        )

    ordered = sorted(records, key=lambda r: r.night_of)
    latest = ordered[-1]
    window = ordered[-DEBT_WINDOW_NIGHTS:]

    debt = sum(
        min(max(target_h - r.duration_h, 0.0), MAX_SHORTFALL_PER_NIGHT) for r in window
    )
    debt = min(round(debt, 2), DEBT_CAP_H)

    sd = _circular_sd_minutes([r.midpoint_min for r in window])
    if len(window) < MIN_NIGHTS_FOR_TREND:
        confidence = Confidence.LOW
        sd = None  # no trend claims below the minimum-n gate
    else:
        confidence = Confidence.HIGH

    return SleepStats(
        nights=len(window),
        duration_h=round(latest.duration_h, 2),
        midpoint_min=latest.midpoint_min,
        debt_h=debt,
        regularity_sd_min=None if sd is None else round(sd, 1),
        confidence=confidence,
    )


def _circular_sd_minutes(minutes: Iterable[int]) -> Optional[float]:
    """Standard deviation on a 24h clock (midnight wrap makes linear SD wrong)."""
    values = list(minutes)
    if len(values) < 2:
        return None
    angles = [v / 1440.0 * 2 * math.pi for v in values]
    sin_sum = sum(math.sin(a) for a in angles)
    cos_sum = sum(math.cos(a) for a in angles)
    n = len(angles)
    r = math.hypot(sin_sum, cos_sum) / n
    if r <= 0:
        return 720.0  # fully dispersed
    r = min(r, 1.0)
    sd_rad = math.sqrt(-2.0 * math.log(r))
    return sd_rad / (2 * math.pi) * 1440.0


def energy_band(
    *,
    reported: Optional[int],
    sleep_h: Optional[float],
    debt_h: Optional[float],
) -> Optional[int]:
    """1..5. Self-report always wins; otherwise derive from sleep, never invent."""
    if reported is not None:
        return max(1, min(5, reported))
    if sleep_h is None:
        return None
    score = 3
    if sleep_h < 5:
        score -= 2
    elif sleep_h < 6.5:
        score -= 1
    elif sleep_h >= 7.5:
        score += 1
    if debt_h is not None and debt_h > 4:
        score -= 1
    return max(1, min(5, score))


def max_effort(energy: Optional[int], sleep_h: Optional[float], debt_h: Optional[float]) -> int:
    """The ceiling the plan may use today. Low confidence never raises the bar."""
    if sleep_h is not None and sleep_h < 5:
        return 1
    if debt_h is not None and debt_h > 3:
        return 1
    if energy is None:
        return 2  # unknown energy: stay conservative
    if energy <= 2:
        return 1
    if energy == 3:
        return 2
    return 3


def is_bad_day(
    *,
    sleep_h: Optional[float],
    energy: Optional[int],
    debt_h: Optional[float],
) -> bool:
    if sleep_h is not None and sleep_h < 5.5:
        return True
    if energy is not None and energy <= 1:
        return True
    if debt_h is not None and debt_h >= 6:
        return True
    return False


def midpoint_label(minutes: Optional[int]) -> Optional[str]:
    if minutes is None:
        return None
    h, m = divmod(minutes % 1440, 60)
    return f"{h:02d}:{m:02d}"


def consistency_rate(statuses: Sequence[str]) -> float:
    """Share of days *attempted* (done or partial). A miss costs nothing extra."""
    if not statuses:
        return 0.0
    attempted = sum(1 for s in statuses if s in ("done", "partial"))
    return round(attempted / len(statuses), 3)
