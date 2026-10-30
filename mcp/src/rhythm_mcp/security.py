"""Token → Principal resolution.

The MCP paper is blunt about two protocol gaps: coarse-grained client-server auth
and ambiguous identity propagation. This module is where Rhythm closes both:

- every call carries `(agent_id, subject, scopes, audience)` — no anonymous calls;
- the token's *audience* must equal this server, so a token minted for another
  service cannot be replayed here (confused deputy, part 1);
- the token's *subject* is the only person the agent can act for (part 2);
- scopes are per-token, not per-client, so least privilege is real.

Token format (dev/build): `subject:scope1,scope2` — deliberately simple so tests
need no JWT machinery. Production swaps `parse_token` for a JWT/OAuth verifier
against the org IdP; nothing else changes.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from rhythm.agent.policy import ALL_SCOPES, Principal, PolicyError

SERVER_AUDIENCE = "rhythm-mcp"


def parse_token(token: str, *, audience: str = SERVER_AUDIENCE) -> Principal:
    """`subject:scope1,scope2[:agent_id]` → Principal. Fails closed."""
    try:
        subject, scope_part, agent_id = (token.split(":") + ["rhythm-mcp-client"])[:3]
    except ValueError:
        raise PolicyError(
            "bad_token", "Malformed token", "Expected 'subject:scopes[:agent_id]'."
        )
    scopes = frozenset(s.strip() for s in scope_part.split(",") if s.strip())
    if not subject or not scopes:
        raise PolicyError("bad_token", "Token has empty subject or scopes")
    unknown = scopes - ALL_SCOPES
    if unknown:
        raise PolicyError("bad_scope", f"Unknown scopes in token: {sorted(unknown)}")
    return Principal(
        agent_id=agent_id,
        subject=subject,
        scopes=scopes,
        audience=audience,
    )


def dev_principal(subject: Optional[str] = None) -> Principal:
    """stdio/local development principal. Never used on the HTTP path."""
    subject = subject or os.environ.get("RHYTHM_DEV_SUBJECT", "dev-user")
    scopes = frozenset(
        s.strip()
        for s in os.environ.get("RHYTHM_DEV_SCOPES", "rhythm.read,rhythm.write,rhythm.safety").split(",")
        if s.strip()
    )
    return Principal(
        agent_id=os.environ.get("RHYTHM_DEV_AGENT", "dev-agent"),
        subject=subject,
        scopes=scopes,
        audience=SERVER_AUDIENCE,
    )


def principal_from_bearer(header_value: Optional[str]) -> Principal:
    if not header_value or not header_value.lower().startswith("bearer "):
        raise PolicyError(
            "unauthenticated",
            "Missing bearer token",
            "Send 'Authorization: Bearer <subject:scopes>'.",
        )
    return parse_token(header_value[7:].strip())
