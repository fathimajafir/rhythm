"""Agent identity and policy (Introduction to Agents, §"Agent Identity", §"Policies).

An agent is a third class of principal: not a user, not a service account. It acts
*on behalf of* a user, so every call carries `(agent_id, subject, scopes, audience)`.

This module is the answer to three risks named in the MCP paper:
- **No per-tool scoping in the protocol** → we enforce scopes ourselves.
- **Confused deputy** → the subject is bound into the call; an agent cannot use its
  own credentials to touch another user's data.
- **Coarse-grained auth** → a token for this audience is refused on any other.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

READ = "rhythm.read"
WRITE = "rhythm.write"
SAFETY = "rhythm.safety"
ADMIN = "rhythm.admin"

ALL_SCOPES = frozenset({READ, WRITE, SAFETY, ADMIN})


class PolicyError(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class Principal:
    agent_id: str
    subject: str  # the human this agent acts for
    scopes: frozenset[str]
    audience: str = "rhythm-mcp"
    expires_at: Optional[float] = None
    workspace: str = "default"

    def check(self) -> None:
        if self.expires_at is not None and self.expires_at < time.time():
            raise PolicyError("expired", "Credential expired", "Re-issue a short-lived token.")
        unknown = self.scopes - ALL_SCOPES
        if unknown:
            raise PolicyError("bad_scope", f"Unknown scopes: {sorted(unknown)}")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


def authorize(
    *,
    principal: Principal,
    required_scopes: Iterable[str],
    tool_name: str,
    arguments: dict,
    subject_arg: str = "user_id",
    audience_required: Optional[str] = None,
    now: Optional[float] = None,
) -> PolicyDecision:
    """Fail closed. Any ambiguity is a denial, never a warning."""
    principal.check()

    if audience_required and principal.audience != audience_required:
        raise PolicyError(
            "audience_mismatch",
            f"Token audience '{principal.audience}' is not '{audience_required}'",
            "Request a token for this server. Tokens must not be replayed across services.",
        )

    missing = set(required_scopes) - set(principal.scopes)
    if missing:
        raise PolicyError(
            "forbidden",
            f"{tool_name} requires {sorted(missing)}",
            "Least privilege: grant only the scope this task needs.",
        )

    claimed = arguments.get(subject_arg)
    if claimed and claimed != principal.subject and ADMIN not in principal.scopes:
        raise PolicyError(
            "subject_mismatch",
            f"This credential acts for '{principal.subject}', not '{claimed}'",
            "An agent cannot use its own credentials to reach another person's data (confused deputy).",
        )
    return PolicyDecision(True)


def visible_tools(specs, principal: Principal) -> tuple[str, ...]:
    """Least privilege applied to the *tool list* itself: the model never sees what
    it cannot call. This is also context engineering — a smaller tool space."""
    out = []
    for spec in specs:
        if set(spec.scopes) <= set(principal.scopes):
            out.append(spec.name)
    return tuple(sorted(out))
