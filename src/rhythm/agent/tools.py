"""The Rhythm tool set. Handlers wrap the deterministic Phase-1 core — no model.

Schema notes follow the MCP whitepaper: `description` fields are written for the
calling model, write tools declare idempotency requirements, list tools paginate.
"""
from __future__ import annotations

from datetime import date, datetime
from .. import coach, safety
from ..domain import CalendarLoad, Locale, Profile, SignalDay, SleepRecord
from ..library import LIBRARY
from ..metrics import sleep_stats
from ..planner import plan_day
from .policy import READ, SAFETY, WRITE
from .registry import Registry, Runtime, ToolContext, ToolSpec, plan_dict, state_dict

DAY_SCHEMA = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}


def _parse_day(args: dict[str, Any]) -> date:
    return date.fromisoformat(args["day"])


def _parse_at(args: dict[str, Any], day: date) -> datetime:
    raw = args.get("at")
    if raw:
        return datetime.fromisoformat(raw)
    return datetime.combine(day, datetime.min.time()).replace(hour=9)


def _signals_from_args(args: dict[str, Any], day: date) -> SignalDay | None:
    raw = args.get("signals")
    if not raw:
        return None
    sleep = raw.get("sleep")
    record = None
    if sleep:
        record = SleepRecord(
            night_of=day,
            bedtime=datetime.fromisoformat(sleep["bedtime"]),
            waketime=datetime.fromisoformat(sleep["waketime"]),
            source=sleep.get("source", "self_report"),
            confidence=float(sleep.get("confidence", 1.0)),
        )
    return SignalDay(
        day=day,
        sleep=record,
        steps=raw.get("steps"),
        active_minutes=raw.get("active_minutes"),
        temp_c=raw.get("temp_c"),
        calendar=CalendarLoad(events=int(raw.get("calendar_events", 0))),
    )


def build_registry(runtime: Runtime | None = None) -> Registry:
    runtime = runtime or Runtime(loop=DayLoop())

    # ------------------------------------------------------------------ read
    # Reads on a person with no profile are honest-empty, not errors: they exist
    # (the subject is bound in the token), they simply have no history yet.
    def _has_profile(args: dict, ctx: ToolContext) -> bool:
        return args["user_id"] in ctx.runtime.loop.store.profiles

    def day_state(args: dict, ctx: ToolContext):
        if not _has_profile(args, ctx):
            return {
                "user_id": args["user_id"],
                "day": args["day"],
                "phase": "idle",
                "locale": "en",
                "energy": None,
                "sleep_h": None,
                "sleep_debt_h": None,
                "regularity_sd_min": None,
                "heat": False,
                "bad_day": False,
                "confidence": "unknown",
                "exists": False,
            }
        return state_dict(ctx.runtime.loop.state(args["user_id"], _parse_day(args)))

    def sleep_summary(args: dict, ctx: ToolContext):
        if not _has_profile(args, ctx):
            return {
                "nights": 0,
                "sleep_h": None,
                "sleep_debt_h": None,
                "regularity_sd_min": None,
                "confidence": "unknown",
                "exists": False,
            }
        profile = ctx.runtime.loop.store.profile(args["user_id"])
        records = ctx.runtime.loop.store.sleep_history.get(args["user_id"], [])
        stats = sleep_stats(records, target_h=profile.sleep_target_h)
        return {
            "nights": stats.nights,
            "sleep_h": stats.duration_h,
            "sleep_debt_h": stats.debt_h,
            "regularity_sd_min": stats.regularity_sd_min,
            "confidence": stats.confidence.value,
        }

    def plan_preview(args: dict, ctx: ToolContext):
        loop = ctx.runtime.loop
        if not _has_profile(args, ctx):
            return {
                "primary": None,
                "optional": None,
                "reason": "no profile yet — run rhythm.checkin first",
                "downgraded": False,
                "proposal": None,
                "exists": False,
            }
        day = _parse_day(args)
        at = _parse_at(args, day)
        state = loop.state(args["user_id"], day)
        if state.phase.value == "idle":
            # A read must not raise because the day hasn't started; say so instead.
            return {
                "primary": None,
                "reason": "not checked in yet",
                "proposal": None,
                "downgraded": False,
            }
        plan = plan_day(state=state, profile=loop.store.profile(args["user_id"]), at=at)
        return plan_dict(plan, state.locale)

    def action_library(args: dict, ctx: ToolContext):
        limit = int(args.get("limit", 20))
        offset = int(args.get("offset", 0))
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
                    "title_en": a.title_en,
                    "title_ar": a.title_ar,
                    "duration_min": a.duration_min,
                    "effort": a.effort,
                    "outdoor": a.outdoor,
                }
                for a in window
            ],
        }

    def safety_classify(args: dict, ctx: ToolContext):
        verdict = safety.classify(args["text"], args.get("region", "AE"))
        if verdict.opens_escalation:
            ctx.runtime.loop.store.log_escalation(
                {
                    "subject": ctx.principal.subject,
                    "band": verdict.band.value,
                    "matched": list(verdict.matched),
                }
            )
        return {
            "band": verdict.band.value,
            "matched": list(verdict.matched),
            "message": verdict.message,
            "opens_escalation": verdict.opens_escalation,
            "blocks_plan": verdict.blocks_plan,
        }

    def review_weekly(args: dict, ctx: ToolContext):
        if not _has_profile(args, ctx):
            return {
                "review": "No check-ins yet — the first week starts with tomorrow morning's check-in.",
                "exists": False,
            }
        text = ctx.runtime.loop.weekly_review(args["user_id"], _parse_day(args))
        return {"review": text, "exists": True}

    # ----------------------------------------------------------------- write
    def checkin(args: dict, ctx: ToolContext):
        loop = ctx.runtime.loop
        if args["user_id"] not in loop.store.profiles:
            loop.register(
                Profile(user_id=args["user_id"], locale=Locale(args.get("locale", "en")))
            )
        state = loop.check_in(
            args["user_id"],
            _parse_day(args),
            _parse_at(args, _parse_day(args)),
            energy=args.get("energy"),
            signals=_signals_from_args(args, _parse_day(args)),
        )
        return state_dict(state)

    def commitment_create(args: dict, ctx: ToolContext):
        loop = ctx.runtime.loop
        day = _parse_day(args)
        commitment = loop.commit(args["user_id"], day, _parse_at(args, day))
        return {
            "commitment_id": commitment.id,
            "action_id": commitment.action_id,
            "title": commitment.title,
            "duration_min": commitment.duration_min,
            "slot": commitment.slot.value,
            "status": commitment.status,
        }

    def commitment_close(args: dict, ctx: ToolContext):
        closed = ctx.runtime.loop.close(
            args["user_id"],
            args["commitment_id"],
            args["status"],
            idempotency_key=args["idempotency_key"],
        )
        return {"commitment_id": closed.id, "status": closed.status}

    # -------------------------------------------------------------- assembly
    return Registry(
        [
            ToolSpec(
                name="rhythm.day.state",
                title="Today's state",
                description="Returns the person's day state: phase, energy, sleep debt, "
                "regularity, heat and confidence. Read-only. Gaps are null, never zero.",
                input_schema={
                    "type": "object",
                    "required": ["user_id", "day"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string", "description": "Subject user id."},
                        "day": {
                            "type": "string",
                            "pattern": r"^\d{4}-\d{2}-\d{2}$",
                            "description": "Local calendar day, YYYY-MM-DD.",
                        },
                    },
                },
                handler=day_state,
                scopes=frozenset({READ}),
            ),
            ToolSpec(
                name="rhythm.sleep.summary",
                title="Sleep summary",
                description="Seven-night sleep summary: last night's duration, estimated "
                "debt and midpoint regularity. confidence is 'low' below four nights — "
                "never claim a trend from low-confidence data.",
                input_schema={
                    "type": "object",
                    "required": ["user_id"],
                    "additionalProperties": False,
                    "properties": {"user_id": {"type": "string"}},
                },
                handler=sleep_summary,
                scopes=frozenset({READ}),
            ),
            ToolSpec(
                name="rhythm.plan.preview",
                title="Preview today's plan",
                description="The single primary action plus one optional, chosen by the "
                "constrained planner. Read-only; nothing is committed. A null primary "
                "means the day has not been checked in.",
                input_schema={
                    "type": "object",
                    "required": ["user_id", "day"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string"},
                        "day": {
                            "type": "string",
                            "pattern": r"^\d{4}-\d{2}-\d{2}$",
                        },
                        "at": {"type": "string", "description": "ISO local datetime"},
                    },
                },
                handler=plan_preview,
                scopes=frozenset({READ}),
            ),
            ToolSpec(
                name="rhythm.action.library",
                title="Action library",
                description="Paginated list of coaching actions with effort, duration and "
                "time-window metadata.",
                input_schema={
                    "type": "object",
                    "required": ["user_id"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "offset": {"type": "integer", "minimum": 0},
                    },
                },
                handler=action_library,
                scopes=frozenset({READ}),
            ),
            ToolSpec(
                name="rhythm.safety.classify",
                title="Safety classification",
                description="Deterministic red-flag/amber classification of free text. "
                "Run BEFORE acting on any user text that could contain symptoms. Red "
                "blocks the plan and opens a clinician escalation.",
                input_schema={
                    "type": "object",
                    "required": ["text"],
                    "additionalProperties": False,
                    "properties": {
                        "text": {"type": "string", "description": "Raw user text."},
                        "region": {"type": "string", "description": "ISO country code for crisis routing."},
                    },
                },
                handler=safety_classify,
                scopes=frozenset({SAFETY}),
            ),
            ToolSpec(
                name="rhythm.review.weekly",
                title="Weekly review",
                description="Deterministic weekly synthesis: consistency, sleep midpoint "
                "and one change for next week. Refuses percentages below three logged days.",
                input_schema={
                    "type": "object",
                    "required": ["user_id", "day"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string"},
                        "day": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                    },
                },
                handler=review_weekly,
                scopes=frozenset({READ}),
            ),
            ToolSpec(
                name="rhythm.checkin",
                title="Record a check-in",
                description="Folds the morning check-in (sleep, energy, environment) into "
                "the day's state. Requires a prior user registration via the admin API or "
                "an existing profile.",
                input_schema={
                    "type": "object",
                    "required": ["user_id", "day", "at"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string"},
                        "day": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                        "at": {"type": "string", "description": "ISO local datetime"},
                        "energy": {"type": "integer", "minimum": 1, "maximum": 5},
                        "locale": {"type": "string", "enum": ["en", "ar"]},
                        "signals": {
                            "type": "object",
                            "description": "Optional; omit unknown fields rather than sending zeros.",
                            "properties": {
                                "steps": {"type": "integer", "minimum": 0},
                                "active_minutes": {"type": "integer", "minimum": 0},
                                "temp_c": {"type": "number"},
                                "calendar_events": {"type": "integer", "minimum": 0},
                                "sleep": {
                                    "type": "object",
                                    "properties": {
                                        "bedtime": {"type": "string"},
                                        "waketime": {"type": "string"},
                                        "source": {"type": "string"},
                                        "confidence": {"type": "number"},
                                    },
                                },
                            },
                        },
                    },
                },
                handler=checkin,
                scopes=frozenset({WRITE}),
                read_only=False,
                idempotent=True,
            ),
            ToolSpec(
                name="rhythm.commitment.create",
                title="Commit to today's action",
                description="Creates the one commitment of the day; returns the existing "
                "open commitment if there already is one. Requires a prior check-in.",
                input_schema={
                    "type": "object",
                    "required": ["user_id", "day", "at"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string"},
                        "day": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                        "at": {"type": "string"},
                    },
                },
                handler=commitment_create,
                scopes=frozenset({WRITE}),
                read_only=False,
            ),
            ToolSpec(
                name="rhythm.commitment.close",
                title="Close a commitment",
                description="Close with status done | partial | miss. Requires "
                "idempotency_key: a replayed call returns the original result and never "
                "double-applies. A miss carries no penalty.",
                input_schema={
                    "type": "object",
                    "required": ["user_id", "commitment_id", "status", "idempotency_key"],
                    "additionalProperties": False,
                    "properties": {
                        "user_id": {"type": "string"},
                        "commitment_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["done", "partial", "miss"]},
                        "idempotency_key": {"type": "string", "minLength": 1},
                    },
                },
                handler=commitment_close,
                scopes=frozenset({WRITE}),
                read_only=False,
                idempotent=True,
                key_required=True,
            ),
        ]
    )
