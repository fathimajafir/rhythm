"""HTTP surface (design doc §7) — the stateful core in deployment.

Every number returned here comes from `metrics.py`; no model is consulted.

Deployment shape: this service is the single stateful writer. The MCP server runs
stateless beside it and proxies here (mcp/src/rhythm_mcp/remote.py). Persistence
is file-backed when RHYTHM_STORE_PATH is set (see persistence.py); an internal
bearer token (RHYTHM_INTERNAL_TOKEN) protects /v1/* when configured.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import coach, safety
from .domain import CalendarLoad, DayState, Locale, Profile, SignalDay, SleepRecord
from .library import LIBRARY
from .loop import DayLoop, TransitionError
from .metrics import sleep_stats
from .persistence import store_from_env

store = store_from_env()
loop = DayLoop(store)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Degrade gracefully and leave a consistent snapshot behind on SIGTERM.
    yield
    if hasattr(store, "snapshot"):
        store.snapshot()


app = FastAPI(title="Rhythm — daily energy & recovery API", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def internal_token_guard(request: Request, call_next):
    """Optional shared-secret between trusted services and this core.

    Enabled only when RHYTHM_INTERNAL_TOKEN is set. Health probes stay open.
    End-user authn arrives with Phase 2 (device tokens); until then the token
    plus network boundaries are the control.
    """
    expected = os.environ.get("RHYTHM_INTERNAL_TOKEN", "")
    if expected and request.url.path.startswith("/v1/"):
        header = request.headers.get("authorization", "")
        if header != f"Bearer {expected}":
            return JSONResponse(
                {"detail": "unauthenticated", "hint": "Send 'Authorization: Bearer <internal token>'."},
                status_code=401,
            )
    return await call_next(request)


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------
class ProfileIn(BaseModel):
    user_id: str
    locale: Locale = Locale.EN
    sleep_target_h: float = 7.5
    constraints: list[str] = Field(default_factory=list)


class SleepIn(BaseModel):
    bedtime: datetime
    waketime: datetime
    source: str = "self_report"
    confidence: float = 1.0


class SignalsIn(BaseModel):
    steps: Optional[int] = None
    active_minutes: Optional[int] = None
    temp_c: Optional[float] = None
    calendar_events: int = 0
    sleep: Optional[SleepIn] = None


class CheckInIn(BaseModel):
    user_id: str
    day: date
    at: datetime
    energy: Optional[int] = Field(default=None, ge=1, le=5)
    signals: Optional[SignalsIn] = None


class CloseIn(BaseModel):
    status: str = Field(pattern="^(done|partial|miss)$")
    idempotency_key: Optional[str] = None


class ReplanIn(BaseModel):
    user_id: str
    day: date
    at: datetime
    reason: str


class ClassifyIn(BaseModel):
    text: str
    region: str = "AE"


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "phase": 1, "model_calls": 0}


@app.post("/v1/users")
def create_user(body: ProfileIn) -> dict:
    profile = Profile(
        user_id=body.user_id,
        locale=body.locale,
        sleep_target_h=body.sleep_target_h,
        constraints=frozenset(body.constraints),
    )
    loop.register(profile)
    return {"user_id": profile.user_id, "locale": profile.locale.value}


@app.post("/v1/checkin")
def check_in(body: CheckInIn) -> dict:
    # First check-in registers the person (device onboarding parity with the
    # MCP tool). Locale defaults to the cohort default; changeable later.
    if body.user_id not in loop.store.profiles:
        loop.register(Profile(user_id=body.user_id, locale=Locale.EN))

    signals = None
    if body.signals is not None:
        s = body.signals
        sleep = None
        if s.sleep is not None:
            sleep = SleepRecord(
                night_of=body.day,
                bedtime=s.sleep.bedtime,
                waketime=s.sleep.waketime,
                source=s.sleep.source,
                confidence=s.sleep.confidence,
            )
        signals = SignalDay(
            day=body.day,
            sleep=sleep,
            steps=s.steps,
            active_minutes=s.active_minutes,
            temp_c=s.temp_c,
            calendar=CalendarLoad(events=s.calendar_events),
        )

    state = loop.check_in(body.user_id, body.day, body.at, energy=body.energy, signals=signals)
    return _state_payload(state)


@app.get("/v1/today/{user_id}")
def today(user_id: str, day: date = Query(...)) -> dict:
    try:
        state = loop.state(user_id, day)
        plan = loop.plan(user_id, day, datetime.combine(day, datetime.min.time()).replace(hour=9))
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown user")
    except TransitionError:
        return {
            "state": _state_payload(state),
            "prompt": coach.checkin_prompt(state.locale),
            "plan": None,
        }
    return {
        "state": _state_payload(state),
        "plan": _plan_payload(plan),
        "proposal": coach.proposal(state.locale, plan),
    }


@app.post("/v1/commitments")
def commit(user_id: str, day: date, at: Optional[datetime] = None) -> dict:
    at = at or datetime.combine(day, datetime.min.time()).replace(hour=9)
    try:
        c = loop.commit(user_id, day, at)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown user")
    except TransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "commitment_id": c.id,
        "action_id": c.action_id,
        "title": c.title,
        "duration_min": c.duration_min,
        "slot": c.slot.value,
        "status": c.status,
        "fallback_offered": c.fallback_offered,
    }


@app.post("/v1/commitments/{commitment_id}/close")
def close(commitment_id: str, body: CloseIn, user_id: str) -> dict:
    try:
        c = loop.close(user_id, commitment_id, body.status, idempotency_key=body.idempotency_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown commitment")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not your commitment")
    return {
        "commitment_id": c.id,
        "status": c.status,
        "ack": coach.close_ack(loop.state(user_id, c.day).locale, c.status),
    }


@app.post("/v1/plan/replan")
def replan(body: ReplanIn) -> dict:
    try:
        plan = loop.plan(body.user_id, body.day, body.at, reason=body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown user")
    except TransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"reason": body.reason, "plan": _plan_payload(plan)}


@app.get("/v1/review/weekly/{user_id}")
def weekly(user_id: str, day: date = Query(...)) -> dict:
    try:
        text = loop.weekly_review(user_id, day)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown user")
    return {"review": text}


@app.get("/v1/sleep/{user_id}")
def sleep_summary(user_id: str) -> dict:
    try:
        profile = loop.store.profile(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown user")
    records = loop.store.sleep_history.get(user_id, [])
    stats = sleep_stats(records, target_h=profile.sleep_target_h)
    return {
        "nights": stats.nights,
        "sleep_h": stats.duration_h,
        "sleep_debt_h": stats.debt_h,
        "regularity_sd_min": stats.regularity_sd_min,
        "confidence": stats.confidence.value,
    }


@app.get("/v1/library/actions")
def action_library(limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)) -> dict:
    window = LIBRARY[offset : offset + limit]
    return {
        "total": len(LIBRARY),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + len(window) if offset + len(window) < len(LIBRARY) else None,
        "actions": [
            {
                "id": a.id,
                "behaviour": a.behaviour.value,
                "slot": a.slot.value,
                "window": list(a.window) if a.window else None,
                "duration_min": a.duration_min,
                "fallback_min": a.fallback_min,
                "effort": a.effort,
                "outdoor": a.outdoor,
                "title_en": a.title_en,
                "title_ar": a.title_ar,
            }
            for a in window
        ],
    }


@app.post("/v1/safety/classify")
def classify(body: ClassifyIn) -> dict:
    verdict = safety.classify(body.text, body.region)
    return {
        "band": verdict.band.value,
        "matched": list(verdict.matched),
        "message": verdict.message,
        "opens_escalation": verdict.opens_escalation,
        "blocks_plan": verdict.blocks_plan,
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _state_payload(state: DayState) -> dict:
    return {
        "user_id": state.user_id,
        "day": state.day.isoformat(),
        "phase": state.phase.value,
        "locale": state.locale.value,
        "energy": state.energy,
        "sleep_h": state.sleep_h,
        "sleep_debt_h": state.sleep_debt_h,
        "regularity_sd_min": state.regularity_sd_min,
        "heat": state.heat,
        "bad_day": state.bad_day,
        "confidence": state.confidence.value,
    }


def _plan_payload(plan) -> Optional[dict]:
    if plan.primary is None:
        return None
    return {
        "primary": {
            "id": plan.primary.id,
            "title_en": plan.primary.title_en,
            "title_ar": plan.primary.title_ar,
            "behaviour": plan.primary.behaviour.value,
            "duration_min": plan.primary.duration_min,
            "effort": plan.primary.effort,
        },
        "optional": plan.optional.id if plan.optional else None,
        "reason": plan.reason,
        "downgraded": plan.downgraded,
    }
