"""The tool registry: one definition, used by the in-process agent *and* the MCP server.

This is the "control plane" idea from *Introduction to Agents* (§"Agent Governance")
reduced to a single process: a tool exists once, with its schema, scopes, side-effect
annotations and version. Everything else — the agent loop, the MCP server, the
evaluator — reads from here. That is what makes the tools reusable instead of
duplicated per transport.

Each spec carries the annotations MCP defines (readOnly / destructive / idempotent /
openWorld) so the two surfaces cannot disagree about what a tool does.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional

from .. import safety
from ..domain import CalendarLoad, SignalDay, SleepRecord
from ..loop import DayLoop, TransitionError
from ..metrics import sleep_stats
from ..planner import plan_day
from ..store import Store
from .. import coach
from .policy import READ, SAFETY, WRITE, Principal, authorize
from .trace import Trace, digest

Handler = Callable[[dict[str, Any], "ToolContext"], Any]


class ToolError(RuntimeError):
    """A tool-level failure with a machine code and a hint the caller can act on."""

    def __init__(self, code: str, message: str, hint: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
        }


@dataclass
class Runtime:
    """Process-wide services the tools need. Injected, never imported globally."""

    loop: DayLoop = field(default_factory=DayLoop)
    idempotency: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    principal: Principal
    runtime: Runtime
    trace: Trace
    request_id: str = ""

    @property
    def user_id(self) -> str:
        return self.principal.subject


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    scopes: frozenset[str]
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = False
    key_required: bool = False  # at-most-once semantics across a network boundary
    open_world: bool = False
    version: str = "1.0.0"
    output_schema: Optional[dict[str, Any]] = None

    def annotations(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }

    def fingerprint(self) -> str:
        payload = f"{self.name}@{self.version}:{self.description}".encode()
        return hashlib.sha256(payload).hexdigest()[:12]


# ---------------------------------------------------------------------------
# a small, explicit schema validator (no third-party dependency)
# ---------------------------------------------------------------------------
_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate(schema: dict[str, Any], value: Any, path: str = "") -> list[str]:
    """Validate the subset of JSON Schema this server actually uses."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if expected and expected in _TYPES and not isinstance(value, _TYPES[expected]):
        # bool is an int in Python; keep the distinction honest
        if not (expected in ("integer", "number") and isinstance(value, bool)):
            return [f"{path or 'value'}: expected {expected}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or 'value'}: must be one of {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}: does not match required format")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"missing required field: {path + '.' if path else ''}{key}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value and value[key] is not None:
                errors.extend(validate(sub, value[key], f"{path + '.' if path else ''}{key}"))
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                errors.append(f"unexpected fields: {sorted(extra)}")
    return errors


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
class Registry:
    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolError(
                "unknown_tool",
                f"Unknown tool: {name}",
                "It may be misspelled, or not granted to this principal. List tools again.",
            )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[n] for n in self.names())

    def manifest(self) -> dict[str, Any]:
        """Namespaced, versioned, hashed — the anti-shadowing identity of this server."""
        body = {s.name: {"version": s.version, "fingerprint": s.fingerprint()} for s in self.specs()}
        canonical = "|".join(f"{k}@{v['version']}:{v['fingerprint']}" for k, v in sorted(body.items()))
        return {
            "server": "rhythm-mcp",
            "protocol": "mcp",
            "tools": body,
            "digest": hashlib.sha256(canonical.encode()).hexdigest(),
        }

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        audience: Optional[str] = None,
    ) -> Any:
        spec = self.get(name)

        with ctx.trace.span(f"tool.{name}", tool=name, args_digest=digest(arguments)) as span:
            authorize(
                principal=ctx.principal,
                required_scopes=spec.scopes,
                tool_name=name,
                arguments=arguments,
                audience_required=audience,
            )

            errors = validate(spec.input_schema, arguments)
            if errors:
                span.status = "error"
                raise ToolError(
                    "invalid_arguments",
                    "; ".join(errors),
                    "Check the tool's inputSchema and retry with corrected fields.",
                )

            key = arguments.get("idempotency_key")
            cache_key = (name, key) if key else None
            if spec.key_required and not key:
                raise ToolError(
                    "idempotency_key_required",
                    f"{name} requires idempotency_key",
                    "Supply a caller-generated key so a retry cannot double-apply.",
                )
            if cache_key and cache_key in ctx.runtime.idempotency:
                span.event("idempotent_replay")
                return ctx.runtime.idempotency[cache_key]

            try:
                result = spec.handler(arguments, ctx)
            except ToolError:
                span.status = "error"
                raise
            except KeyError as exc:
                span.status = "error"
                raise ToolError("not_found", f"Not found: {exc}", "Check the id in the request.")
            except PermissionError as exc:
                span.status = "error"
                raise ToolError("forbidden", str(exc), "This object belongs to another subject.")
            except TransitionError as exc:
                span.status = "error"
                raise ToolError(
                    "wrong_state", str(exc), "Check in before planning or committing."
                )
            except Exception as exc:  # noqa: BLE001
                span.status = "error"
                raise ToolError(
                    "internal", f"{type(exc).__name__}: {exc}", "Retry; if it persists, page on-call.",
                    retryable=True,
                )

            if cache_key:
                ctx.runtime.idempotency[cache_key] = result
            return result


# ---------------------------------------------------------------------------
# serialisers (kept next to the tools so both surfaces return identical shapes)
# ---------------------------------------------------------------------------
def state_dict(state) -> dict[str, Any]:
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


def plan_dict(plan, locale) -> dict[str, Any]:
    if plan.primary is None:
        return {"primary": None, "optional": None, "reason": plan.reason, "downgraded": plan.downgraded}
    return {
        "primary": {
            "id": plan.primary.id,
            "title_en": plan.primary.title_en,
            "title_ar": plan.primary.title_ar,
            "behaviour": plan.primary.behaviour.value,
            "duration_min": plan.primary.duration_min,
            "effort": plan.primary.effort,
            "outdoor": plan.primary.outdoor,
        },
        "optional": plan.optional.id if plan.optional else None,
        "reason": plan.reason,
        "downgraded": plan.downgraded,
        "proposal": coach.proposal(locale, plan),
    }


def _parse_day(args: dict[str, Any]) -> date:
    return date.fromisoformat(args["day"])


def _parse_at(args: dict[str, Any], day: date) -> datetime:
    raw = args.get("at")
    return datetime.fromisoformat(raw) if raw else datetime.combine(day, datetime.min.time()).replace(hour=9)
