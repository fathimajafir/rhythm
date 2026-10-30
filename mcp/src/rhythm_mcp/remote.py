"""Remote registry: the MCP server as a stateless gateway.

Deployment shape (docs/deploy/production.md): the API is the single stateful
writer; MCP containers are stateless and scale horizontally. When
`RHYTHM_API_URL` is set, this registry serves the *same* tool schemas, scopes and
manifest as the in-process one — so drift tests and MCP clients see an identical
surface — while every call proxies to the API over HTTP.

Policy is still enforced here first (scopes, subject binding, audience), then
again by the API's own guard. Defence in depth, not duplication: the MCP layer
stops cross-subject access before it ever reaches the network.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from rhythm.agent.policy import authorize
from rhythm.agent.registry import Registry, Runtime, ToolContext, ToolError
from rhythm.agent.tools import build_registry
from rhythm.jsonschema_lite import validate


class RemoteRegistry(Registry):
    def __init__(
        self,
        base_url: str,
        *,
        internal_token: str = "",
        timeout_s: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        # Schemas, annotations and the manifest come from the same single source
        # of truth; only dispatch differs.
        specs = build_registry(Runtime()).specs()
        super().__init__(specs)
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token or os.environ.get("RHYTHM_INTERNAL_TOKEN", "")
        self.timeout_s = timeout_s
        self._transport = transport

    # ------------------------------------------------------------------ call
    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        audience: Optional[str] = None,
    ) -> Any:
        spec = self.get(name)
        authorize(
            principal=ctx.principal,
            required_scopes=spec.scopes,
            tool_name=name,
            arguments=arguments,
            audience_required=audience,
        )
        errors = validate(spec.input_schema, arguments)
        if errors:
            raise ToolError(
                "invalid_arguments",
                "; ".join(errors),
                "Check the tool's inputSchema and retry with corrected fields.",
            )
        return self._dispatch(name, dict(arguments))

    # -------------------------------------------------------------- dispatch
    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.internal_token:
            headers["authorization"] = f"Bearer {self.internal_token}"
        return headers

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                resp = client.request(
                    method, f"{self.base_url}{path}", params=params, json=json_body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ToolError(
                "upstream_unavailable",
                f"Rhythm API unreachable: {exc}",
                "Retry shortly; the failure is on the service path, not your input.",
                retryable=True,
            )
        if resp.status_code == 404:
            raise _NotFound(resp.text[:200])
        if resp.status_code == 403:
            raise ToolError("forbidden", "The API rejected this subject.", "Do not retry as-is.")
        if resp.status_code == 409:
            raise ToolError(
                "wrong_state", "The day is not in the right state for that.",
                "Check in first, then retry.", retryable=False,
            )
        if resp.status_code == 422:
            raise ToolError(
                "invalid_arguments", f"API rejected the payload: {resp.text[:200]}",
                "Correct the fields named in the message.",
            )
        if resp.status_code >= 400:
            raise ToolError(
                "internal", f"API error {resp.status_code}: {resp.text[:200]}",
                "Retry; if it persists, page on-call.", retryable=True,
            )
        return resp.json()

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        try:
            return self._route(name, args)
        except _NotFound as exc:
            return self._not_found_payload(name, args, exc)

    def _route(self, name: str, args: dict[str, Any]) -> Any:
        user_id = args.get("user_id", "")
        day = args.get("day", "")

        if name == "rhythm.checkin":
            body = {k: v for k, v in args.items() if k != "locale"}
            return self._request("POST", "/v1/checkin", json_body=body)

        if name == "rhythm.commitment.create":
            params = {"user_id": user_id, "day": day}
            if args.get("at"):
                params["at"] = args["at"]
            return self._request("POST", "/v1/commitments", params=params)

        if name == "rhythm.commitment.close":
            return self._request(
                "POST",
                f"/v1/commitments/{args['commitment_id']}/close",
                params={"user_id": user_id},
                json_body={
                    "status": args["status"],
                    "idempotency_key": args.get("idempotency_key"),
                },
            )

        if name == "rhythm.day.state":
            body = self._request("GET", f"/v1/today/{user_id}", params={"day": day})
            return {**body.get("state", {}), "exists": True}

        if name == "rhythm.plan.preview":
            try:
                body = self._request("GET", f"/v1/today/{user_id}", params={"day": day})
            except _NotFound:
                raise
            plan = body.get("plan")
            if not plan:
                return {
                    "primary": None, "optional": None, "downgraded": False,
                    "reason": "not checked in yet — run rhythm.checkin first",
                    "proposal": None, "exists": True,
                }
            return {**plan, "proposal": body.get("proposal"), "exists": True}

        if name == "rhythm.sleep.summary":
            return self._request("GET", f"/v1/sleep/{user_id}")

        if name == "rhythm.review.weekly":
            return self._request(
                "GET", f"/v1/review/weekly/{user_id}", params={"day": day}
            )

        if name == "rhythm.safety.classify":
            return self._request(
                "POST", "/v1/safety/classify",
                json_body={"text": args["text"], "region": args.get("region", "AE")},
            )

        if name == "rhythm.action.library":
            return self._request(
                "GET", "/v1/library/actions",
                params={"limit": args.get("limit", 20), "offset": args.get("offset", 0)},
            )

        raise ToolError("unknown_tool", f"No remote route for {name}")

    def _not_found_payload(self, name: str, args: dict[str, Any], exc: "_NotFound") -> Any:
        """Reads on a person we have not met are honest-empty, not errors."""
        user_id = args.get("user_id", "")
        if name == "rhythm.day.state":
            return {
                "user_id": user_id, "day": args.get("day", ""), "phase": "idle",
                "locale": "en", "energy": None, "sleep_h": None, "sleep_debt_h": None,
                "regularity_sd_min": None, "active_minutes": None, "heat": False,
                "bad_day": False, "confidence": "unknown", "exists": False,
            }
        if name == "rhythm.plan.preview":
            return {
                "primary": None, "optional": None, "downgraded": False,
                "reason": "no profile yet — run rhythm.checkin first",
                "proposal": None, "exists": False,
            }
        if name == "rhythm.sleep.summary":
            return {
                "nights": 0, "sleep_h": None, "sleep_debt_h": None,
                "regularity_sd_min": None, "confidence": "unknown", "exists": False,
            }
        if name == "rhythm.review.weekly":
            return {
                "review": "No check-ins yet — the first week starts with tomorrow morning's check-in.",
                "exists": False,
            }
        raise ToolError("not_found", str(exc), "Check the id in the request.")


class _NotFound(Exception):
    pass
