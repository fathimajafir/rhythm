"""File-backed persistence for the Phase-1 store.

The design doc is honest that the pilot runs a single stateful core (§12: the
data wall requires separate databases eventually — that is a later milestone).
This module keeps the *promise* that matters in production now: **a restart must
not lose a person's history**.

Design:
- write-through: every mutation snapshots atomically (tmp file + `os.replace`);
- the snapshot is small (300 users × ~30 events is a few MB), so this is cheap;
- a corrupt or unreadable snapshot is quarantined (`.corrupt`) and the store
  starts fresh — never crash-loop on bad data;
- JSON, not pickle: the file outlives code versions.

Horizontal scaling stays out of scope *on purpose*: two writers on one JSON file
would race. The deployment model is one stateful API + stateless MCP gateways
(see docs/deploy/production.md). A real database replaces this behind the same
`Store` interface in the next milestone.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date, datetime, time
from typing import Any, Optional

from .domain import (
    CalendarLoad,
    Commitment,
    Confidence,
    DayState,
    Locale,
    Phase,
    Profile,
    SignalDay,
    Slot,
    SleepRecord,
)
from .store import Store

_SNAPSHOT_VERSION = 1


def _dump(obj: Any) -> Any:
    if isinstance(obj, Profile):
        return {
            "user_id": obj.user_id,
            "locale": obj.locale.value,
            "wake_target": obj.wake_target.isoformat(),
            "sleep_target_h": obj.sleep_target_h,
            "constraints": sorted(obj.constraints),
            "movement_target_min_week": obj.movement_target_min_week,
        }
    if isinstance(obj, DayState):
        return {
            "user_id": obj.user_id,
            "day": obj.day.isoformat(),
            "phase": obj.phase.value,
            "locale": obj.locale.value,
            "energy": obj.energy,
            "sleep_h": obj.sleep_h,
            "sleep_midpoint_min": obj.sleep_midpoint_min,
            "sleep_debt_h": obj.sleep_debt_h,
            "regularity_sd_min": obj.regularity_sd_min,
            "active_minutes": obj.active_minutes,
            "heat": obj.heat,
            "dense_calendar": obj.dense_calendar,
            "confidence": obj.confidence.value,
            "bad_day": obj.bad_day,
        }
    if isinstance(obj, Commitment):
        return {
            "id": obj.id,
            "user_id": obj.user_id,
            "day": obj.day.isoformat(),
            "action_id": obj.action_id,
            "title": obj.title,
            "duration_min": obj.duration_min,
            "slot": obj.slot.value,
            "status": obj.status,
            "fallback_offered": obj.fallback_offered,
        }
    if isinstance(obj, SleepRecord):
        return {
            "night_of": obj.night_of.isoformat(),
            "bedtime": obj.bedtime.isoformat(),
            "waketime": obj.waketime.isoformat(),
            "source": obj.source,
            "confidence": obj.confidence,
        }
    if isinstance(obj, SignalDay):
        return {
            "day": obj.day.isoformat(),
            "sleep": _dump(obj.sleep) if obj.sleep else None,
            "steps": obj.steps,
            "active_minutes": obj.active_minutes,
            "hrv_ms": obj.hrv_ms,
            "resting_hr": obj.resting_hr,
            "calendar": {
                "events": obj.calendar.events,
                "first_start": obj.calendar.first_start.isoformat() if obj.calendar.first_start else None,
                "last_end": obj.calendar.last_end.isoformat() if obj.calendar.last_end else None,
            },
            "temp_c": obj.temp_c,
        }
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_dump(v) for v in obj]
    return obj


def _time(value: Any) -> Optional[time]:
    return time.fromisoformat(value) if value else None


def _profile(body: dict) -> Profile:
    return Profile(
        user_id=body["user_id"],
        locale=Locale(body["locale"]),
        wake_target=time.fromisoformat(body["wake_target"]),
        sleep_target_h=body["sleep_target_h"],
        constraints=frozenset(body["constraints"]),
        movement_target_min_week=body["movement_target_min_week"],
    )


def _day_state(body: dict) -> DayState:
    return DayState(
        user_id=body["user_id"],
        day=date.fromisoformat(body["day"]),
        phase=Phase(body["phase"]),
        locale=Locale(body["locale"]),
        energy=body["energy"],
        sleep_h=body["sleep_h"],
        sleep_midpoint_min=body["sleep_midpoint_min"],
        sleep_debt_h=body["sleep_debt_h"],
        regularity_sd_min=body["regularity_sd_min"],
        active_minutes=body["active_minutes"],
        heat=body["heat"],
        dense_calendar=body["dense_calendar"],
        confidence=Confidence(body["confidence"]),
        bad_day=body["bad_day"],
    )


def _commitment(body: dict) -> Commitment:
    return Commitment(
        id=body["id"],
        user_id=body["user_id"],
        day=date.fromisoformat(body["day"]),
        action_id=body["action_id"],
        title=body["title"],
        duration_min=body["duration_min"],
        slot=Slot(body["slot"]),
        status=body["status"],
        fallback_offered=body["fallback_offered"],
    )


def _sleep_record(body: dict) -> SleepRecord:
    return SleepRecord(
        night_of=date.fromisoformat(body["night_of"]),
        bedtime=datetime.fromisoformat(body["bedtime"]),
        waketime=datetime.fromisoformat(body["waketime"]),
        source=body["source"],
        confidence=body["confidence"],
    )


def _signal_day(body: dict) -> SignalDay:
    cal = body["calendar"]
    return SignalDay(
        day=date.fromisoformat(body["day"]),
        sleep=_sleep_record(body["sleep"]) if body.get("sleep") else None,
        steps=body["steps"],
        active_minutes=body["active_minutes"],
        hrv_ms=body["hrv_ms"],
        resting_hr=body["resting_hr"],
        calendar=CalendarLoad(
            events=cal["events"],
            first_start=_time(cal["first_start"]),
            last_end=_time(cal["last_end"]),
        ),
        temp_c=body["temp_c"],
    )


class SavedStore(Store):
    """A `Store` that snapshots to disk after every mutation."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self._lock = threading.Lock()
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, encoding="utf-8") as fh:
                body = json.load(fh)
            if body.get("version") != _SNAPSHOT_VERSION:
                raise ValueError(f"snapshot version {body.get('version')!r} unsupported")
            self._restore(body)
            return True
        except Exception:
            # Quarantine, never crash-loop on bad data.
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return False

    def _restore(self, body: dict) -> None:
        self.profiles = {k: _profile(v) for k, v in body["profiles"].items()}
        self.days = {
            (k.split("|", 1)[0], date.fromisoformat(k.split("|", 1)[1])): _day_state(v)
            for k, v in body["days"].items()
        }
        self.commitments = {k: _commitment(v) for k, v in body["commitments"].items()}
        self.signals = {
            (k.split("|", 1)[0], date.fromisoformat(k.split("|", 1)[1])): _signal_day(v)
            for k, v in body["signals"].items()
        }
        self.sleep_history = {
            k: [_sleep_record(r) for r in v] for k, v in body["sleep_history"].items()
        }
        self.close_keys = dict(body["close_keys"])
        self.escalations = list(body["escalations"])

    def snapshot(self) -> None:
        body = {
            "version": _SNAPSHOT_VERSION,
            "saved_at": datetime.now().isoformat(),
            "profiles": {k: _dump(v) for k, v in self.profiles.items()},
            "days": {
                f"{u}|{d.isoformat()}": _dump(v) for (u, d), v in self.days.items()
            },
            "commitments": {k: _dump(v) for k, v in self.commitments.items()},
            "signals": {
                f"{u}|{d.isoformat()}": _dump(v) for (u, d), v in self.signals.items()
            },
            "sleep_history": {k: [_dump(r) for r in v] for k, v in self.sleep_history.items()},
            "close_keys": dict(self.close_keys),
            "escalations": [_dump(e) for e in self.escalations],
        }
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(body, fh, ensure_ascii=False)
                os.replace(tmp, self.path)  # atomic on POSIX
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ------------------------------------------------------ hooked mutators
    def put_profile(self, profile: Profile) -> Profile:
        super().put_profile(profile)
        self.snapshot()
        return profile

    def put_signals(self, user_id: str, day: SignalDay) -> None:
        super().put_signals(user_id, day)
        self.snapshot()

    def add_sleep(self, user_id: str, record: SleepRecord) -> None:
        super().add_sleep(user_id, record)
        self.snapshot()

    def put_day(self, state: DayState) -> DayState:
        super().put_day(state)
        self.snapshot()
        return state

    def put_commitment(self, commitment: Commitment) -> Commitment:
        super().put_commitment(commitment)
        self.snapshot()
        return commitment

    def put_close_key(self, key: str, commitment_id: str) -> None:
        self.close_keys[key] = commitment_id
        self.snapshot()

    def log_escalation(self, payload: dict) -> dict:
        super().log_escalation(payload)
        self.snapshot()
        return payload


def store_from_env() -> Store:
    """Build the process store from the environment.

    `RHYTHM_STORE_PATH` set → durable file-backed store (recommended in Docker).
    Unset → the in-memory Phase-1 store (tests, demos).
    """
    path = os.environ.get("RHYTHM_STORE_PATH")
    return SavedStore(path) if path else Store()
