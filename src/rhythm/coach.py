"""Templated coach: Phase 1 runs with *no model at all*.

This is deliberately boring. It proves the loop end-to-end before any LLM exists,
and it remains the fallback whenever the model layer is down or over budget.
Every string is authored in both languages — not translated at runtime.
"""
from __future__ import annotations

from typing import Optional

from .domain import Commitment, DayState, Locale, Plan, SleepStats
from .metrics import consistency_rate, midpoint_label

MAX_WORDS = 60


def checkin_prompt(locale: Locale) -> str:
    if locale is Locale.AR:
        return "صباح الخير. كيف كانت نومتك، وطاقتك من ١ إلى ٥؟ سطر واحد يكفي."
    return "Morning. How did you sleep, and how's your energy from 1 to 5? One line is enough."


def proposal(locale: Locale, plan: Plan) -> str:
    if plan.primary is None:
        if locale is Locale.AR:
            return "لا أملك ما يكفي لأخطط اليوم. سطر واحد: كيف حالك؟"
        return "I don't have enough to plan today. One line: how are you?"

    action = plan.primary
    title = action.title_ar if locale is Locale.AR else action.title_en
    anchor = action.anchor_ar if locale is Locale.AR else action.anchor_en

    if locale is Locale.AR:
        lead = "اليومُ أخفّ: " if plan.downgraded else "اليوم: "
        tail = f" — {action.duration_min} دقيقة" + (f"، {anchor}" if anchor else "")
        return f"{lead}{title}{tail}."

    lead = "Today the bar is lower: " if plan.downgraded else "Today: "
    tail = f" — {action.duration_min} min" + (f", {anchor}" if anchor else "")
    return f"{lead}{title}{tail}."


def nudge(locale: Locale, commitment: Commitment, minutes_left: int) -> str:
    if locale is Locale.AR:
        return f"الآن هو وقت: {commitment.title}."
    return f"Now's the window: {commitment.title}."


def close_ack(locale: Locale, status: str) -> str:
    table = {
        "done": (
            "سُجّل. هذا يكفي اليوم.",
            "Logged. That's the day.",
        ),
        "partial": (
            "النصف يُحسب. نفس الوقت غدًا.",
            "Half counts. Same time tomorrow.",
        ),
        "miss": (
            "لا عقاب. غدًا نعود.",
            "No penalty. Tomorrow we go again.",
        ),
    }
    ar, en = table.get(status, table["miss"])
    return ar if locale is Locale.AR else en


def unknown_data_note(locale: Locale) -> str:
    if locale is Locale.AR:
        return "لا أملك نوم الليلة الماضية — لا ساعة؟ أخبرني بسطر واحد."
    return "I don't have last night's sleep — no band today? Tell me in one line."


def low_confidence_note(locale: Locale, nights: int) -> str:
    if locale is Locale.AR:
        return f"لديّ {nights} ليالٍ فقط. لن أتحدث عن اتجاه قبل أربع."
    return f"I only have {nights} nights. I won't claim a trend before four."


def behaviour_label(locale: Locale, behaviour) -> str:
    """The behaviour, not a time-bound action — a weekly change outlives 9am."""
    labels = {
        "sleep": ("انتظام النوم", "protect your wake time"),
        "movement": ("الحركة اليومية", "move a little every day"),
        "winddown": ("التهدئة قبل النوم", "a real wind-down"),
    }
    key = getattr(behaviour, "value", behaviour)
    ar, en = labels.get(key, (key, key))
    return ar if locale is Locale.AR else en


def weekly_review(
    locale: Locale,
    *,
    state: DayState,
    stats: SleepStats,
    statuses: list[str],
    next_action: Optional[str],
) -> str:
    """Three bullets, one change. Deterministic — no model in Phase 1."""
    rate = consistency_rate(statuses)
    midpoint = midpoint_label(stats.midpoint_min)
    lines: list[str] = []

    # Minimum-n honesty: a percentage from one logged day is a lie told politely.
    few_days = len(statuses) < 3

    if locale is Locale.AR:
        if few_days:
            lines.append(f"سجّلت {len(statuses)} يومًا فقط — مبكر جدًا لأي اتجاه.")
        else:
            lines.append(f"أنجزت {int(round(rate * 100))}% من الأيام التي حاولت فيها.")
        if midpoint and not few_days:
            lines.append(f"منتصف نومك كان حوالي {midpoint}.")
        if stats.debt_h is not None and not few_days:
            lines.append(f"دين النوم التقديري: {stats.debt_h} ساعة.")
        if next_action:
            lines.append(f"الأسبوع القادم: تغيير واحد فقط — {next_action}.")
    else:
        if few_days:
            lines.append(
                f"Only {len(statuses)} day{'s' if len(statuses) != 1 else ''} logged so far — "
                "too early for a trend."
            )
        else:
            lines.append(f"You completed {int(round(rate * 100))}% of the days you attempted.")
        if midpoint and not few_days:
            lines.append(f"Your sleep midpoint sat around {midpoint}.")
        if stats.debt_h is not None and not few_days:
            lines.append(f"Estimated sleep debt: {stats.debt_h}h.")
        if next_action:
            lines.append(f"Next week: one change only — {next_action}.")

    return " ".join(lines)
