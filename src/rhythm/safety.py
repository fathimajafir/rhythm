"""Deterministic safety router. Sits *outside* the model and fails closed.

Design doc §9: red flags never reach the generator. The response shape is fixed,
and the escalation opens in the same breath as the message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class SafetyBand(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class Verdict:
    band: SafetyBand
    matched: tuple[str, ...]
    message: Optional[str] = None
    opens_escalation: bool = False
    blocks_plan: bool = False


# Hard red flags: no model in the path, no judgement call.
RED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bchest (pain|pressure|tightness)\b", "chest pain"),
    (r"\bcan'?t breathe\b|\bshort(ness)? of breath\b", "breathlessness"),
    (r"\bfaint(ed|ing)?\b|\bpassed out\b", "fainting"),
    (r"\bface droop|\bslurred speech|\bweak(ness)? on one side\b", "stroke signs"),
    (r"\bsuicid|\bkill myself|\bend my life|\bself[- ]harm", "self-harm"),
    (r"\beat(ing)? disorder|\bpurg(e|ing)\b|\bbing(e|ing) and vomit", "eating disorder"),
    (r"\bstopped (taking )?(my )?(medication|meds|tablets)\b", "medication stopped"),
    (r"\bbleeding\b.*\bpregnan|\bpregnan.*\bbleeding\b", "pregnancy bleeding"),
)

# Soft risk: lower the intensity, offer a human, change nothing else.
AMBER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bpalpitation|\bheart racing\b", "palpitations"),
    (r"\bdizzy|\blight[- ]headed\b", "dizziness"),
    (r"\bcan'?t sleep for (days|weeks)\b|\bhaven'?t slept\b", "severe insomnia"),
    (r"\bexhausted (for|all) (weeks|month)", "persistent fatigue"),
    (r"\bhate my body\b|\bbody image\b", "body image"),
    (r"\bburn(t|ed)? out\b", "burnout"),
)

# Regional routing. Clinical ops MUST populate these before launch — an empty
# entry is reported as "needs_region_config" rather than guessed.
EMERGENCY_NUMBERS: dict[str, str] = {"AE": "999"}
CRISIS_LINES: dict[str, str] = {}  # intentionally empty until clinical sign-off


def classify(text: str, region: str = "AE") -> Verdict:
    lowered = text.lower()
    red = tuple(label for pattern, label in RED_PATTERNS if re.search(pattern, lowered))
    if red:
        return Verdict(
            band=SafetyBand.RED,
            matched=red,
            message=red_message(region),
            opens_escalation=True,
            blocks_plan=True,
        )

    amber = tuple(label for pattern, label in AMBER_PATTERNS if re.search(pattern, lowered))
    if amber:
        return Verdict(
            band=SafetyBand.AMBER,
            matched=amber,
            message=amber_message(),
            opens_escalation=True,
            blocks_plan=False,
        )

    return Verdict(band=SafetyBand.GREEN, matched=())


def red_message(region: str) -> str:
    emergency = EMERGENCY_NUMBERS.get(region)
    crisis = CRISIS_LINES.get(region)
    if not emergency:
        routing = "Contact your local emergency number now."
    else:
        routing = f"Call {emergency} now if this is urgent."
    if crisis:
        routing += f" For emotional crisis support: {crisis}."
    return (
        f"Please stop and get real help now — this needs a clinician, not a coaching app. "
        f"{routing} I'm pausing your plan until a clinician has spoken with you."
    )


def amber_message() -> str:
    return (
        "That's worth a proper conversation. I'll keep today light and a clinician "
        "will follow up with you. Nothing else changes in your plan."
    )


def scan_many(texts: Iterable[str], region: str = "AE") -> Verdict:
    """Fail closed: the first red verdict wins."""
    worst = Verdict(band=SafetyBand.GREEN, matched=())
    for text in texts:
        verdict = classify(text, region)
        if verdict.band is SafetyBand.RED:
            return verdict
        if verdict.band is SafetyBand.AMBER:
            worst = verdict
    return worst
