"""Remote registry tests: the MCP server as a stateless gateway to the API.

The gateway must be a faithful mirror of the in-process registry — same schemas,
same honest-empty reads, same policy — with HTTP dispatch in between. All HTTP is
mocked; nothing here needs a running API.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "mcp" / "src")]

from rhythm.agent import build_registry  # noqa: E402
from rhythm.agent.policy import PolicyError, Principal, READ  # noqa: E402
from rhythm.agent.registry import Runtime, ToolContext, ToolError  # noqa: E402
from rhythm.agent.trace import Trace  # noqa: E402
from rhythm_mcp.remote import RemoteRegistry  # noqa: E402

DAY = "2026-10-26"


def _principal(subject="hana", scopes=frozenset({"rhythm.read", "rhythm.write", "rhythm.safety"})):
    return Principal(agent_id="test-agent", subject=subject, scopes=scopes)


def _ctx(principal=None):
    return ToolContext(principal=principal or _principal(), runtime=Runtime(), trace=Trace("t"))


def _api(handler) -> RemoteRegistry:
    return RemoteRegistry(
        "http://api.test", internal_token="secret", transport=httpx.MockTransport(handler)
    )


def test_same_surface_as_the_local_registry():
    local = build_registry(Runtime())
    remote = _api(lambda r: httpx.Response(200, json={}))
    assert remote.names() == local.names()
    assert remote.manifest()["digest"] == local.manifest()["digest"]
    for spec in local.specs():
        assert remote.get(spec.name).input_schema == spec.input_schema
        assert remote.get(spec.name).annotations() == spec.annotations()


def test_policy_runs_before_the_network():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        return httpx.Response(200, json={})

    remote = _api(handler)
    # the token acts for someone else → denied locally, API never called
    with pytest.raises(PolicyError) as err:
        remote.call(
            "rhythm.day.state",
            {"user_id": "someone-else", "day": DAY},
            _ctx(_principal(subject="hana")),
        )
    assert err.value.code == "subject_mismatch"
    assert "called" not in seen


def test_invalid_arguments_rejected_before_the_network():
    def handler(request):
        raise AssertionError("must not reach the API")

    remote = _api(handler)
    with pytest.raises(ToolError) as err:
        remote.call("rhythm.day.state", {"user_id": "hana"}, _ctx())  # missing day
    assert err.value.code == "invalid_arguments"


def test_checkin_forwards_body_and_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"user_id": "hana", "phase": "checked_in"})

    remote = _api(handler)
    out = remote.call(
        "rhythm.checkin",
        {
            "user_id": "hana", "day": DAY, "at": f"{DAY}T08:00:00", "energy": 3,
            "locale": "ar",  # locale is MCP-only; must not confuse the API
            "signals": {"temp_c": 41.0, "sleep": {"bedtime": f"{DAY}T00:30:00", "waketime": f"{DAY}T06:30:00"}},
        },
        _ctx(),
    )
    assert seen["path"] == "/v1/checkin"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["user_id"] == "hana"
    assert "locale" not in seen["body"]
    assert seen["body"]["signals"]["temp_c"] == 41.0
    assert out["phase"] == "checked_in"


def test_commitment_create_uses_query_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"], seen["params"] = (
            request.method, request.url.path, dict(request.url.params),
        )
        return httpx.Response(200, json={"commitment_id": "c1", "status": "open"})

    remote = _api(handler)
    out = remote.call(
        "rhythm.commitment.create",
        {"user_id": "hana", "day": DAY, "at": f"{DAY}T09:00:00"},
        _ctx(),
    )
    assert (seen["method"], seen["path"]) == ("POST", "/v1/commitments")
    assert seen["params"]["user_id"] == "hana"
    assert out["commitment_id"] == "c1"


def test_close_forwards_idempotency_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"commitment_id": "c1", "status": "done"})

    remote = _api(handler)
    remote.call(
        "rhythm.commitment.close",
        {"user_id": "hana", "commitment_id": "c1", "status": "done", "idempotency_key": "k-1"},
        _ctx(),
    )
    assert seen["path"] == "/v1/commitments/c1/close"
    assert seen["params"] == {"user_id": "hana"}
    assert seen["body"] == {"status": "done", "idempotency_key": "k-1"}


def test_day_state_merges_exists_true():
    def handler(request):
        return httpx.Response(200, json={
            "state": {"user_id": "hana", "phase": "checked_in", "sleep_h": 6.0},
            "plan": None, "proposal": None,
        })

    remote = _api(handler)
    out = remote.call("rhythm.day.state", {"user_id": "hana", "day": DAY}, _ctx())
    assert out["exists"] is True
    assert out["sleep_h"] == 6.0


def test_unknown_person_reads_are_honest_empty():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404, json={"detail": "unknown user"})

    remote = _api(handler)

    state = remote.call("rhythm.day.state", {"user_id": "hana", "day": DAY}, _ctx())
    assert state["phase"] == "idle" and state["exists"] is False and state["confidence"] == "unknown"

    plan = remote.call("rhythm.plan.preview", {"user_id": "hana", "day": DAY}, _ctx())
    assert plan["primary"] is None and "no profile yet" in plan["reason"]

    sleep = remote.call("rhythm.sleep.summary", {"user_id": "hana"}, _ctx())
    assert sleep["nights"] == 0 and sleep["sleep_h"] is None

    review = remote.call("rhythm.review.weekly", {"user_id": "hana", "day": DAY}, _ctx())
    assert "No check-ins yet" in review["review"]


def test_plan_preview_merges_the_proposal():
    def handler(request):
        return httpx.Response(200, json={
            "state": {"phase": "checked_in"},
            "plan": {"primary": {"id": "m_stretch_5"}, "reason": "r", "downgraded": False},
            "proposal": "Today: Desk stretch — 5 min.",
        })

    remote = _api(handler)
    out = remote.call("rhythm.plan.preview", {"user_id": "hana", "day": DAY}, _ctx())
    assert out["primary"]["id"] == "m_stretch_5"
    assert out["proposal"].startswith("Today:")


def test_api_conflict_maps_to_wrong_state():
    remote = _api(lambda r: httpx.Response(409, json={"detail": "check in before committing"}))
    with pytest.raises(ToolError) as err:
        remote.call(
            "rhythm.commitment.create",
            {"user_id": "hana", "day": DAY, "at": f"{DAY}T09:00:00"},
            _ctx(),
        )
    assert err.value.code == "wrong_state"
    assert "Check in first" in err.value.hint


def test_api_outage_is_retryable_guidance():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    remote = _api(handler)
    with pytest.raises(ToolError) as err:
        remote.call("rhythm.safety.classify", {"text": "slept fine"}, _ctx())
    assert err.value.code == "upstream_unavailable"
    assert err.value.retryable is True


def test_unregistered_day_state_without_day_fails_validation_locally():
    remote = _api(lambda r: httpx.Response(500, json={}))
    with pytest.raises(ToolError) as err:
        remote.call("rhythm.safety.classify", {}, _ctx())  # missing text
    assert err.value.code == "invalid_arguments"
