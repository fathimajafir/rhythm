"""Persistence tests: restarts must not lose a person's history.

Covers the roundtrip of every stored shape, the quarantine of corrupt snapshots,
and the env factory. No Docker, no network — a plain file.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rhythm.domain import (  # noqa: E402
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
from rhythm.loop import DayLoop  # noqa: E402
from rhythm.persistence import SavedStore, store_from_env  # noqa: E402

DAY = date(2026, 10, 26)


def _rich_store(tmp_path: Path) -> tuple[SavedStore, Path]:
    path = tmp_path / "store.json"
    store = SavedStore(str(path))
    loop = DayLoop(store)

    loop.register(
        Profile(
            user_id="hana",
            locale=Locale.AR,
            wake_target=time(6, 30),
            constraints=frozenset({"knee", "pregnancy"}),
        )
    )
    signals = SignalDay(
        day=DAY,
        sleep=SleepRecord(
            night_of=DAY,
            bedtime=datetime(2026, 10, 25, 23, 30),
            waketime=datetime(2026, 10, 26, 5, 30),
            source="healthkit",
            confidence=0.9,
        ),
        steps=4200,
        active_minutes=12,
        temp_c=41.5,
        calendar=CalendarLoad(events=6, first_start=time(9, 0), last_end=time(18, 30)),
    )
    # energy=1 + a hot day: bad-day mode fires, so the commitment is downgraded
    loop.check_in("hana", DAY, datetime(2026, 10, 26, 8, 0), energy=1, signals=signals)
    commitment = loop.commit("hana", DAY, datetime(2026, 10, 26, 9, 0))
    loop.close("hana", commitment.id, "partial", idempotency_key="key-1")
    store.log_escalation({"user_id": "hana", "band": "amber", "matched": ["dizziness"]})
    return store, path


def test_roundtrip_preserves_every_stored_shape(tmp_path):
    store, path = _rich_store(tmp_path)

    reloaded = SavedStore(str(path))
    # profile
    assert reloaded.profiles["hana"].locale is Locale.AR
    assert reloaded.profiles["hana"].wake_target == time(6, 30)
    assert reloaded.profiles["hana"].constraints == frozenset({"knee", "pregnancy"})
    # day state
    state = reloaded.days[("hana", DAY)]
    assert state.phase is Phase.CLOSED
    assert state.sleep_h == 6.0
    assert state.heat is True
    assert state.confidence is Confidence.LOW  # one night: the min-n gate
    # commitment
    (commitment,) = reloaded.commitments.values()
    assert commitment.status == "partial"
    assert commitment.slot is Slot.MORNING
    assert commitment.fallback_offered is True
    # signals + sleep
    signals = reloaded.signals[("hana", DAY)]
    assert signals.sleep is not None and signals.sleep.source == "healthkit"
    assert signals.calendar.first_start == time(9, 0)
    # close keys survive restart → idempotent replay after a restart
    assert reloaded.close_keys == {"key-1": commitment.id}
    reloaded_closed = reloaded.get_commitment(commitment.id)
    assert reloaded_closed.status == "partial"
    # escalations
    assert reloaded.escalations[0]["band"] == "amber"


def test_replayed_close_after_restart_returns_original(tmp_path):
    store, path = _rich_store(tmp_path)
    (commitment_id, *_), = [(c.id,) for c in store.commitments.values()]

    reloaded = SavedStore(str(path))
    loop = DayLoop(reloaded)
    again = loop.close("hana", commitment_id, "miss", idempotency_key="key-1")
    assert again.status == "partial"  # the original result wins, not the retry


def test_corrupt_snapshot_is_quarantined_not_crash_looping(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{ this is not json")
    store = SavedStore(str(path))
    assert store.profiles == {}  # fresh start
    assert Path(str(path) + ".corrupt").exists()  # evidence preserved
    # and the store still works afterwards
    store.put_profile(Profile(user_id="x"))
    assert "x" in store.profiles


def test_snapshot_is_atomic_no_temp_litter(tmp_path):
    path = tmp_path / "store.json"
    store = SavedStore(str(path))
    store.put_profile(Profile(user_id="x"))
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".snapshot-")]
    assert leftovers == []
    assert json.loads(path.read_text())["version"] == 1


def test_store_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("RHYTHM_STORE_PATH", raising=False)
    assert type(store_from_env()) is not SavedStore  # in-memory

    monkeypatch.setenv("RHYTHM_STORE_PATH", str(tmp_path / "s.json"))
    assert isinstance(store_from_env(), SavedStore)


def test_full_day_loop_survives_a_restart_mid_flight(tmp_path):
    path = tmp_path / "store.json"

    first = DayLoop(SavedStore(str(path)))
    first.register(Profile(user_id="hana"))
    first.check_in("hana", DAY, datetime(2026, 10, 26, 8, 0), energy=3)
    commitment = first.commit("hana", DAY, datetime(2026, 10, 26, 9, 0))

    # process "restarts"
    second = DayLoop(SavedStore(str(path)))
    assert second.state("hana", DAY).phase is Phase.COMMITTED
    nudge = second.nudge("hana", DAY, at=datetime(2026, 10, 26, 10, 0))
    assert nudge is not None
    closed = second.close("hana", commitment.id, "done")
    assert closed.status == "done"
    review = second.weekly_review("hana", DAY)
    assert "1 day" in review
