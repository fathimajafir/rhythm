"""MCP integration tests against a live streamable-HTTP server.

Validates what the whitepaper calls out:
- tools listed with full definitions and honest annotations;
- tool errors return isError with code + next step (guidance for the caller's model);
- unknown tools are protocol errors;
- auth: missing token, bad scope, and confused-deputy subject binding all fail closed;
- idempotent replay of commitment.close;
- resources + prompts primitives;
- exposed schemas cannot drift from the registry (single source of truth).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "src", ROOT / "mcp" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from rhythm.agent import build_registry  # noqa: E402
from rhythm.agent.policy import READ, PolicyError, authorize  # noqa: E402
from rhythm.agent.registry import Runtime  # noqa: E402
from rhythm_mcp.security import parse_token, principal_from_bearer  # noqa: E402

DAY = "2026-10-26"
TOKEN = "smoke:rhythm.read,rhythm.write,rhythm.safety:pytest-agent"
MCP_TO_REGISTRY = {
    "rhythm_day_state": "rhythm.day.state",
    "rhythm_sleep_summary": "rhythm.sleep.summary",
    "rhythm_plan_preview": "rhythm.plan.preview",
    "rhythm_action_library": "rhythm.action.library",
    "rhythm_safety_classify": "rhythm.safety.classify",
    "rhythm_review_weekly": "rhythm.review.weekly",
    "rhythm_checkin": "rhythm.checkin",
    "rhythm_commitment_create": "rhythm.commitment.create",
    "rhythm_commitment_close": "rhythm.commitment.close",
}


@pytest.fixture(scope="module")
def live():
    from rhythm_mcp.server import build_http_app

    app, registry = build_http_app(Runtime())
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/mcp", registry
    server.should_exit = True
    thread.join(timeout=5)


def _payload(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


@asynccontextmanager
async def mcp_session(base_url: str, token: str | None = TOKEN):
    """One MCP session over the real HTTP transport. The pattern proven above."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = httpx2.AsyncClient(headers=headers, timeout=15)
    try:
        async with streamable_http_client(base_url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as sess:
                await asyncio.wait_for(sess.initialize(), 15)
                yield sess
    finally:
        await client.aclose()


async def test_lists_all_tools_with_honest_annotations(live):
    url, _ = live
    async with mcp_session(url) as sess:
        result = await sess.list_tools()
        names = {t.name for t in result.tools}
        assert set(MCP_TO_REGISTRY) <= names

        close = next(t for t in result.tools if t.name == "rhythm_commitment_close")
        assert close.annotations.read_only_hint is False
        assert close.annotations.idempotent_hint is True
        assert close.description  # the paper: description "should always be included"

        classify = next(t for t in result.tools if t.name == "rhythm_safety_classify")
        assert classify.annotations.read_only_hint is True


# The MCP adapter deliberately flattens one nested object for client ergonomics.
# Everything else must match the registry exactly (required sets, properties, annotations).
KNOWN_FLATTENED = {"rhythm.checkin": {"signals"}}


async def test_no_schema_drift_between_registry_and_mcp(live):
    """One definition, two surfaces — they must agree or the build fails."""
    url, registry = live
    async with mcp_session(url) as sess:
        listed = await sess.list_tools()
        by_name = {t.name: t for t in listed.tools}

        for mcp_name, reg_name in MCP_TO_REGISTRY.items():
            spec = registry.get(reg_name)
            schema = by_name[mcp_name].input_schema
            assert set(schema.get("required", [])) == set(spec.input_schema.get("required", [])), reg_name
            flattened = KNOWN_FLATTENED.get(reg_name, set())
            registry_props = set(spec.input_schema.get("properties", {})) - flattened
            mcp_props = set(schema.get("properties", {}))
            missing = registry_props - mcp_props
            assert not missing, f"{reg_name}: registry fields missing from MCP schema: {missing}"
            assert by_name[mcp_name].annotations.read_only_hint == spec.read_only, reg_name
            assert by_name[mcp_name].annotations.idempotent_hint == spec.idempotent, reg_name


async def test_tool_error_carries_code_and_next_step(live):
    url, _ = live
    async with mcp_session(url) as sess:
        result = await sess.call_tool(
            "rhythm_commitment_close",
            {"user_id": "smoke", "commitment_id": "does-not-exist", "status": "done"},
        )
        assert result.is_error is True
        text = result.content[0].text
        assert "idempotency_key" in text  # the missing field is named, per the whitepaper


async def test_unknown_tool_is_an_error_result(live):
    url, _ = live
    async with mcp_session(url) as sess:
        result = await sess.call_tool("rhythm_does_not_exist", {})
        assert result.is_error is True
        assert "Unknown tool" in result.content[0].text


async def test_missing_token_fails_closed(live):
    url, _ = live
    async with mcp_session(url, token=None) as sess:
        result = await sess.call_tool("rhythm_safety_classify", {"text": "ok"})
        assert result.is_error is True
        assert "bearer" in result.content[0].text.lower()


async def test_confused_deputy_is_denied(live):
    """A valid token for 'intruder' cannot act for 'smoke'."""
    url, _ = live
    async with mcp_session(url, token="intruder:rhythm.read,rhythm.write") as sess:
        result = await sess.call_tool("rhythm_day_state", {"user_id": "smoke", "day": DAY})
        assert result.is_error is True
        assert "subject_mismatch" in result.content[0].text


async def test_red_flag_blocks_and_routes(live):
    url, _ = live
    async with mcp_session(url) as sess:
        payload = _payload(
            await sess.call_tool(
                "rhythm_safety_classify", {"text": "I have chest pain", "region": "AE"}
            )
        )
        assert payload["band"] == "red"
        assert payload["blocks_plan"] is True
        assert "999" in payload["message"]


async def test_full_loop_with_idempotent_replay(live):
    url, _ = live
    async with mcp_session(url) as sess:
        state = _payload(
            await sess.call_tool(
                "rhythm_checkin",
                {
                    "user_id": "smoke",
                    "day": DAY,
                    "at": f"{DAY}T08:00:00",
                    "energy": 3,
                    "temp_c": 43.0,
                    "sleep_bedtime": f"{DAY}T00:30:00",
                    "sleep_waketime": f"{DAY}T06:30:00",
                },
            )
        )
        assert state["sleep_h"] == 6.0
        assert state["heat"] is True

        plan = _payload(
            await sess.call_tool("rhythm_plan_preview", {"user_id": "smoke", "day": DAY})
        )
        assert plan["primary"]["outdoor"] is False  # 43°C: nothing outdoor

        created = _payload(
            await sess.call_tool(
                "rhythm_commitment_create",
                {"user_id": "smoke", "day": DAY, "at": f"{DAY}T09:00:00"},
            )
        )
        cid = created["commitment_id"]

        first = _payload(
            await sess.call_tool(
                "rhythm_commitment_close",
                {"user_id": "smoke", "commitment_id": cid, "status": "done", "idempotency_key": "k-1"},
            )
        )
        # same key, different intent: the original result must win
        replay = _payload(
            await sess.call_tool(
                "rhythm_commitment_close",
                {"user_id": "smoke", "commitment_id": cid, "status": "miss", "idempotency_key": "k-1"},
            )
        )
        assert first["status"] == replay["status"] == "done"

        # closing again without a key is refused before it reaches the tool
        no_key = await sess.call_tool(
            "rhythm_commitment_close",
            {"user_id": "smoke", "commitment_id": cid, "status": "done"},
        )
        assert no_key.is_error is True
        assert "idempotency_key" in no_key.content[0].text


async def test_resources_and_prompts(live):
    url, _ = live
    async with mcp_session(url) as sess:
        manifest = await sess.read_resource("rhythm://manifest")
        assert "digest" in manifest.contents[0].text

        library = await sess.read_resource("rhythm://library/actions")
        assert "m_walk_10" in library.contents[0].text

        prompt = await sess.get_prompt("daily_checkin", {"locale": "ar"})
        assert "صباح الخير" in prompt.messages[0].content.text


def test_health_and_manifest_endpoints(live):
    url, registry = live
    base = url.rsplit("/mcp", 1)[0]
    body = json.loads(urllib.request.urlopen(f"{base}/healthz").read())
    assert body["ok"] is True
    manifest = json.loads(urllib.request.urlopen(f"{base}/.well-known/rhythm-manifest.json").read())
    assert manifest["digest"]
    assert set(manifest["tools"]) == set(registry.names())


# ---------------------------------------------------------------- unit tests
def test_token_parsing_and_validation():
    p = parse_token("hana:rhythm.read,rhythm.write:rhythm-coach-1")
    assert p.subject == "hana"
    assert p.scopes == frozenset({"rhythm.read", "rhythm.write"})
    assert p.agent_id == "rhythm-coach-1"

    with pytest.raises(PolicyError):
        parse_token("hana:rhythm.root")


def test_bearer_header_required():
    with pytest.raises(PolicyError):
        principal_from_bearer(None)
    with pytest.raises(PolicyError):
        principal_from_bearer("Basic abc")


def test_subject_binding_blocks_confused_deputy():
    p = parse_token("hana:rhythm.read,rhythm.write")
    with pytest.raises(PolicyError) as err:
        authorize(
            principal=p,
            required_scopes=[READ],
            tool_name="rhythm.day.state",
            arguments={"user_id": "someone-else", "day": DAY},
        )
    assert err.value.code == "subject_mismatch"
