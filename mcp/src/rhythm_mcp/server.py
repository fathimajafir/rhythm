"""Rhythm MCP server: one tool registry, exposed over MCP.

Design (from *Agent Tools & Interoperability with MCP*, May 2026):

- **Single source of truth.** Tools are defined once in `rhythm.agent.tools`; this
  server adapts them to MCP. A drift test fails if the exposed signatures and the
  registry schemas ever disagree.
- **Full tool definitions.** name, title, description, inputSchema — description is
  written for the calling model, not for a human (the paper: title/description
  "should always be included").
- **Honest annotations.** readOnly / destructive / idempotent hints come from the
  same ToolSpec the agent layer uses.
- **Identity on every call.** The principal is resolved per request (bearer on
  HTTP, dev token on stdio) and enforced by the registry's policy layer.
- **Errors as guidance.** Tool failures return isError with code + next step,
  so the client model can recover instead of guessing.
"""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import TextContent, ToolAnnotations

from mcp.server.mcpserver.exceptions import ToolError as SDKToolError

from rhythm.agent import build_registry
from rhythm.agent.policy import PolicyError
from rhythm.agent.registry import Runtime, ToolContext, ToolError
from rhythm.agent.trace import Trace

from .errors import as_tool_message
from .security import SERVER_AUDIENCE, dev_principal, principal_from_bearer

SERVER_NAME = "rhythm"
SERVER_VERSION = "1.0.0"


def _principal(ctx: Context):
    """HTTP: the bearer token. stdio: the dev principal (local machine only)."""
    request = getattr(ctx.request_context, "request", None)
    if request is not None:
        header = request.headers.get("authorization")
        return principal_from_bearer(header)
    return dev_principal()


def _make_registry(runtime: Runtime | None):
    """In-process runtime locally; a stateless gateway when RHYTHM_API_URL is set."""
    api_url = os.environ.get("RHYTHM_API_URL", "")
    if api_url:
        from .remote import RemoteRegistry

        return RemoteRegistry(api_url), Runtime()
    runtime = runtime or Runtime()
    return build_registry(runtime), runtime


def build_server(runtime: Runtime | None = None) -> MCPServer:
    registry, runtime = _make_registry(runtime)

    server = MCPServer(
        name=SERVER_NAME,
        title="Rhythm — daily energy & recovery tools",
        instructions=(
            "Tools for a daily health & wellness coach. Read tools are safe; the two "
            "write tools follow the loop: checkin -> commitment.create -> commitment.close. "
            "Always classify safety BEFORE acting on user text that could contain symptoms; "
            "a red band means stop and escalate to a clinician. Gaps in data are null — "
            "never treat them as zero."
        ),
        version=SERVER_VERSION,
    )

    def call_tool(spec_name: str, arguments: dict[str, Any], ctx: Context) -> dict[str, Any]:
        try:
            principal = _principal(ctx)
        except PolicyError as exc:
            # Auth failures are guidance too: say what was wrong and what to do.
            raise SDKToolError(f"[{exc.code}] {exc.message} Next step: {exc.hint}") from exc

        with Trace(f"mcp.{spec_name}") as trace:
            tool_ctx = ToolContext(
                principal=principal, runtime=runtime, trace=trace, request_id=str(ctx.request_id or "")
            )
            try:
                result = registry.call(spec_name, arguments, tool_ctx, audience=SERVER_AUDIENCE)
            except ToolError as exc:
                # isError + a message that tells the calling model what to do next.
                # The SDK's ToolError becomes CallToolResult(is_error=True) with the
                # message intact — that is the guidance channel for the caller's model.
                raise SDKToolError(as_tool_message(exc)) from exc
            except PolicyError as exc:
                # The registry's policy layer (scopes, subject binding, audience).
                raise SDKToolError(f"[{exc.code}] {exc.message} Next step: {exc.hint}") from exc
            trace.finish("ok")
            return result if isinstance(result, dict) else {"data": result}


    from mcp.types import ToolAnnotations as TA

    def _annotations(spec):
        return TA(
            title=spec.title,
            read_only_hint=spec.read_only,
            destructive_hint=spec.destructive,
            idempotent_hint=spec.idempotent,
            open_world_hint=spec.open_world,
        )

    # -- reads ---------------------------------------------------------------
    @server.tool(
        name="rhythm_day_state",
        title="Today's state",
        description=registry.get("rhythm.day.state").description,
        annotations=_annotations(registry.get("rhythm.day.state")),
    )
    def rhythm_day_state(user_id: str, day: str, ctx: Context) -> dict[str, Any]:
        """Day state: phase, energy, sleep debt, regularity, heat, confidence."""
        return call_tool("rhythm.day.state", {"user_id": user_id, "day": day}, ctx)

    @server.tool(
        name="rhythm_sleep_summary",
        title="Sleep summary",
        description=registry.get("rhythm.sleep.summary").description,
        annotations=_annotations(registry.get("rhythm.sleep.summary")),
    )
    def rhythm_sleep_summary(user_id: str, ctx: Context) -> dict[str, Any]:
        """Seven-night sleep summary with an explicit confidence field."""
        return call_tool("rhythm.sleep.summary", {"user_id": user_id}, ctx)

    @server.tool(
        name="rhythm_plan_preview",
        title="Preview today's plan",
        description=registry.get("rhythm.plan.preview").description,
        annotations=_annotations(registry.get("rhythm.plan.preview")),
    )
    def rhythm_plan_preview(user_id: str, day: str, at: str | None = None, ctx: Context = None) -> dict[str, Any]:
        """The one primary action plus one optional. Nothing is committed."""
        args: dict[str, Any] = {"user_id": user_id, "day": day}
        if at:
            args["at"] = at
        return call_tool("rhythm.plan.preview", args, ctx)

    @server.tool(
        name="rhythm_action_library",
        title="Action library",
        description=registry.get("rhythm.action.library").description,
        annotations=_annotations(registry.get("rhythm.action.library")),
    )
    def rhythm_action_library(
        user_id: str, limit: int = 20, offset: int = 0, ctx: Context = None
    ) -> dict[str, Any]:
        """Paginated coaching actions with effort and time-window metadata."""
        return call_tool(
            "rhythm.action.library",
            {"user_id": user_id, "limit": limit, "offset": offset},
            ctx,
        )

    @server.tool(
        name="rhythm_safety_classify",
        title="Safety classification",
        description=registry.get("rhythm.safety.classify").description,
        annotations=_annotations(registry.get("rhythm.safety.classify")),
    )
    def rhythm_safety_classify(text: str, region: str = "AE", ctx: Context = None) -> dict[str, Any]:
        """Deterministic red/amber/green band. Run before acting on symptom text."""
        return call_tool("rhythm.safety.classify", {"text": text, "region": region}, ctx)

    @server.tool(
        name="rhythm_review_weekly",
        title="Weekly review",
        description=registry.get("rhythm.review.weekly").description,
        annotations=_annotations(registry.get("rhythm.review.weekly")),
    )
    def rhythm_review_weekly(user_id: str, day: str, ctx: Context = None) -> dict[str, Any]:
        """Deterministic weekly synthesis; refuses percentages below three days."""
        return call_tool("rhythm.review.weekly", {"user_id": user_id, "day": day}, ctx)

    # -- writes --------------------------------------------------------------
    @server.tool(
        name="rhythm_checkin",
        title="Record a check-in",
        description=registry.get("rhythm.checkin").description,
        annotations=_annotations(registry.get("rhythm.checkin")),
    )
    def rhythm_checkin(
        user_id: str,
        day: str,
        at: str,
        energy: int | None = None,
        locale: str = "en",
        steps: int | None = None,
        active_minutes: int | None = None,
        temp_c: float | None = None,
        calendar_events: int | None = None,
        sleep_bedtime: str | None = None,
        sleep_waketime: str | None = None,
        sleep_source: str = "self_report",
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Morning check-in; advances the day to checked_in. Unknown fields: omit them."""
        signals: dict[str, Any] = {}
        if steps is not None:
            signals["steps"] = steps
        if active_minutes is not None:
            signals["active_minutes"] = active_minutes
        if temp_c is not None:
            signals["temp_c"] = temp_c
        if calendar_events is not None:
            signals["calendar_events"] = calendar_events
        if sleep_bedtime and sleep_waketime:
            signals["sleep"] = {
                "bedtime": sleep_bedtime,
                "waketime": sleep_waketime,
                "source": sleep_source,
            }
        args: dict[str, Any] = {
            "user_id": user_id,
            "day": day,
            "at": at,
            "locale": locale,
        }
        if energy is not None:
            args["energy"] = energy
        if signals:
            args["signals"] = signals
        return call_tool("rhythm.checkin", args, ctx)

    @server.tool(
        name="rhythm_commitment_create",
        title="Commit to today's action",
        description=registry.get("rhythm.commitment.create").description,
        annotations=_annotations(registry.get("rhythm.commitment.create")),
    )
    def rhythm_commitment_create(user_id: str, day: str, at: str, ctx: Context = None) -> dict[str, Any]:
        """The one commitment of the day. Requires a prior check-in."""
        return call_tool(
            "rhythm.commitment.create", {"user_id": user_id, "day": day, "at": at}, ctx
        )

    @server.tool(
        name="rhythm_commitment_close",
        title="Close a commitment",
        description=registry.get("rhythm.commitment.close").description,
        annotations=_annotations(registry.get("rhythm.commitment.close")),
    )
    def rhythm_commitment_close(
        user_id: str,
        commitment_id: str,
        status: str,
        idempotency_key: str,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """done | partial | miss. idempotency_key is required; replays are safe."""
        return call_tool(
            "rhythm.commitment.close",
            {
                "user_id": user_id,
                "commitment_id": commitment_id,
                "status": status,
                "idempotency_key": idempotency_key,
            },
            ctx,
        )

    # ------------------------------------------------------------- resources
    @server.resource(
        "rhythm://manifest",
        name="Tool manifest",
        description="Namespaced, versioned, hashed tool manifest. Clients should pin "
        "the digest; a change means re-review before calling (anti tool-shadowing).",
        mime_type="application/json",
    )
    def tool_manifest() -> str:
        return json.dumps(registry.manifest(), indent=1)

    @server.resource(
        "rhythm://library/actions",
        name="Action library",
        description="The full coaching action library with time windows and fallbacks.",
        mime_type="application/json",
    )
    def action_library() -> str:
        from rhythm.library import LIBRARY

        return json.dumps(
            [
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
                for a in LIBRARY
            ],
            ensure_ascii=False,
            indent=1,
        )

    # --------------------------------------------------------------- prompts
    @server.prompt(
        name="daily_checkin",
        title="Daily check-in conversation",
        description="A 60-second check-in opener that respects the one-action rule.",
    )
    def daily_checkin(locale: str = "en") -> list:
        ar = locale.lower().startswith("ar")
        text = (
            "صباح الخير. كيف كانت نومتك، وطاقتك من ١ إلى ٥؟ سطر واحد يكفي."
            if ar
            else "Morning. How did you sleep, and how's your energy from 1 to 5? One line is enough."
        )
        return [{"role": "user", "content": TextContent(type="text", text=text)}]

    @server.prompt(
        name="weekly_review",
        title="Weekly review conversation",
        description="Close the week: what changed, what it felt like, one change next week.",
    )
    def weekly_review(locale: str = "en") -> list:
        return [
            {
                "role": "user",
                "content": TextContent(
                    type="text",
                    text=(
                        "أرني الأسبوع: ماذا تغيّر، وكيف شعرت، وتغيير واحد للأسبوع القادم."
                        if locale.lower().startswith("ar")
                        else "Show me my week: what changed, how it felt, and one change for next week."
                    ),
                ),
            }
        ]

    return server


def build_http_app(runtime: Runtime | None = None):
    """Starlette app: MCP at /mcp (stateless → horizontal scaling) + ops endpoints.

    /healthz     — liveness
    /readyz      — registry built
    /.well-known/rhythm-manifest.json — the anti-shadowing digest
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    registry, runtime = _make_registry(runtime)
    server = build_server(runtime)
    app = server.streamable_http_app(stateless_http=True)

    async def healthz(request):
        return JSONResponse({"ok": True, "server": SERVER_NAME, "version": SERVER_VERSION})

    async def readyz(request):
        return JSONResponse({"ok": True, "tools": len(registry.names())})

    async def manifest(request):
        return JSONResponse(registry.manifest())

    app.router.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    app.router.routes.insert(0, Route("/readyz", readyz, methods=["GET"]))
    app.router.routes.insert(0, Route("/.well-known/rhythm-manifest.json", manifest, methods=["GET"]))
    return app, registry
