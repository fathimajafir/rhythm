"""AgentOps tracing (Introduction to Agents, §"Debug with OpenTelemetry Traces").

Every step, tool call, policy decision and latency is recorded as a span, in a shape
that maps 1:1 onto OpenTelemetry so it can be exported later without a rewrite.

Two rules:
- **Redaction by default.** Arguments are logged as a digest, never as PII.
- **Replay is possible.** A trace must be enough to reconstruct what the agent saw.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# Keys whose values never enter a trace (design §11, MCP paper §"Sensitive information Leaks").
SENSITIVE_KEYS = frozenset(
    {
        "text",
        "raw_text",
        "note",
        "bedtime",
        "waketime",
        "email",
        "phone",
        "token",
        "authorization",
        "api_key",
        "password",
    }
)


def digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k.lower() in SENSITIVE_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


@dataclass
class Span:
    name: str
    span_id: str
    parent_id: Optional[str]
    started_at: float
    ended_at: Optional[float] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "unset"  # unset | ok | error

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return round((end - self.started_at) * 1000, 2)

    def event(self, name: str, **payload: Any) -> None:
        self.events.append({"event": name, **redact(payload)})

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


class Trace:
    """A single agent run. `trace_id` is propagated to the MCP layer as a header."""

    def __init__(self, name: str = "agent.run") -> None:
        self.trace_id = uuid.uuid4().hex[:16]
        self.spans: list[Span] = []
        self._stack: list[str] = []
        self._root = self._open(name, None)

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish("error" if exc_type else "ok")

    @property
    def current(self) -> Optional[Span]:
        return next((s for s in self.spans if s.span_id == self._stack[-1]), None) if self._stack else None

    def _open(self, name: str, parent: Optional[str]) -> Span:
        span = Span(
            name=name,
            span_id=uuid.uuid4().hex[:12],
            parent_id=parent,
            started_at=time.time(),
        )
        self.spans.append(span)
        self._stack.append(span.span_id)
        return span

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else None
        span = self._open(name, parent)
        span.attributes.update({k: v for k, v in attributes.items()})
        try:
            yield span
            span.status = "ok"
        except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
            span.status = "error"
            span.events.append({"event": "exception", "type": type(exc).__name__})
            raise
        finally:
            span.ended_at = time.time()
            if self._stack and self._stack[-1] == span.span_id:
                self._stack.pop()

    def event(self, name: str, **payload: Any) -> None:
        if self.current is not None:
            self.current.events.append({"event": name, **redact(payload)})

    def finish(self, status: str = "ok") -> None:
        self._root.ended_at = time.time()
        self._root.status = status
        self._stack.clear()

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "spans": [s.as_dict() for s in self.spans],
        }

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "spans": len(self.spans),
            "errors": sum(1 for s in self.spans if s.status == "error"),
            "total_ms": round(sum(s.duration_ms for s in self.spans if s.parent_id is None), 2),
        }
