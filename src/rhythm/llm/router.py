"""Model routing: task → model chain, with budgets and reasoning escalation.

Design doc §5.2 said "match the model to the decision". This module is that rule,
made executable and model-agnostic:

- **Routes name tasks, not models.** A route points at a *capability tier*; the
  concrete model id lives in config, so swapping vendors is a config change.
- **Fallback chains.** Provider down or rate-limited → next model in the chain →
  finally a signal the caller can use to degrade deterministically.
- **Escalation, not default.** Reasoning models are the *escalation* tier: the
  daily loop runs on a fast model, and a step escalates to deep reasoning only
  when it fails, hits length, or the task says so (§"escalate when stuck").
- **Reasoning tokens are output tokens.** The budget counts them, and a hard
  daily cap degrades to the deterministic planner instead of failing the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .capabilities import ModelSpec, Latency, all_specs, resolve
from .compiler import Compiled, compile_request
from .messages import LLMRequest, LLMResponse, Usage
from .providers.base import Provider, ProviderError


class RoutingExhausted(RuntimeError):
    """Every model in the chain failed. The caller should degrade, not retry."""


class BudgetExceeded(RuntimeError):
    """The tenant's cap is spent. Degrade to deterministic, never fail the user."""


@dataclass(frozen=True)
class TaskRoute:
    task: str
    primary: str          # model id (capability registry key)
    fallbacks: tuple[str, ...] = ()
    max_cost_usd: float = 0.05   # per single call
    allow_escalation: bool = False


# Capability tiers, not model ids: "small" = fast/cheap, "mid" = balanced,
# "deep" = reasoning. resolve() maps these aliases through the registry.
TIER_ALIASES: dict[str, str] = {
    "small": "openai/gpt-4.1-mini",
    "mid": "anthropic/claude-haiku-4-5",
    "deep": "anthropic/claude-sonnet-4-5",
}


def resolve_alias(model_ref: str) -> str:
    return TIER_ALIASES.get(model_ref, model_ref)


@dataclass
class Budget:
    """Per-tenant spend guard. Reasoning tokens are output tokens — counted."""

    limit_usd_per_day: float = 0.05
    spent_usd: float = 0.0

    def check(self, estimate: float) -> None:
        if self.spent_usd + estimate > self.limit_usd_per_day:
            raise BudgetExceeded(
                f"daily cap ${self.limit_usd_per_day:.2f} reached "
                f"(spent ${self.spent_usd:.4f}, next call ~${estimate:.4f})"
            )

    def record(self, cost: float) -> None:
        self.spent_usd = round(self.spent_usd + cost, 6)


DEFAULT_ROUTES: dict[str, TaskRoute] = {
    # High volume, narrow labels: never pay for reasoning here.
    "triage": TaskRoute("triage", "small", max_cost_usd=0.01, allow_escalation=False),
    # The daily conversation: fast model, escalate to deep reasoning if stuck.
    "coach": TaskRoute(
        "coach", "mid",
        fallbacks=("deep",),
        max_cost_usd=0.08, allow_escalation=True,
    ),
    # Weekly synthesis is batch/off-peak: latency doesn't matter, quality does.
    "review": TaskRoute("review", "mid", max_cost_usd=0.10, allow_escalation=True),
    # Symptom text deserves real reasoning before a human sees it.
    "safety_review": TaskRoute("safety_review", "deep", max_cost_usd=0.15, allow_escalation=False),
}


@dataclass
class Router:
    providers: dict[str, Provider]
    routes: dict[str, TaskRoute] = field(default_factory=lambda: dict(DEFAULT_ROUTES))
    budget: Budget = field(default_factory=Budget)
    model_overrides: dict[str, str] = field(default_factory=dict)  # tier -> model id

    # ------------------------------------------------------------------ api
    def complete(self, task: str, req: LLMRequest, *, escalate_on: tuple[str, ...] = ("length",)) -> LLMResponse:
        route = self.routes.get(task) or TaskRoute(task, "mid")
        chain: list[str] = [route.primary, *route.fallbacks]
        errors: list[str] = []

        for index, tier in enumerate(chain):
            model_ref = self.model_overrides.get(tier, resolve_alias(tier))
            spec = resolve(model_ref)
            provider = self._provider_for(spec)
            if provider is None:
                errors.append(f"{spec.id}: no provider registered for '{spec.provider}'")
                continue

            est = self._estimate(spec, req)
            try:
                self.budget.check(est)
            except BudgetExceeded:
                if index == len(chain) - 1:
                    raise
                errors.append("budget: skipping to fallback")
                continue

            compiled = compile_request(req, spec, task=task)
            try:
                response = provider.complete(req, spec, compiled)
            except ProviderError as exc:
                errors.append(f"{spec.id}: {exc.code} {exc.message[:120]}")
                if not exc.retryable and exc.code == "provider_rejected":
                    continue  # config-level failure: next model
                continue
            self._record(spec, response.usage)

            if response.finish_reason in escalate_on and route.allow_escalation:
                escalated = self._escalate(task, route, req, errors)
                if escalated is not None:
                    return escalated
            return response

        raise RoutingExhausted("; ".join(errors) or "no providers configured")

    # -------------------------------------------------------------- helpers
    def _provider_for(self, spec: ModelSpec) -> Optional[Provider]:
        return self.providers.get(spec.provider)

    def _estimate(self, spec: ModelSpec, req: LLMRequest) -> float:
        # ~4 chars/token is plenty for a cap check; providers report exact usage.
        chars = sum(len(m.text()) for m in req.messages)
        input_tokens = max(1, chars // 4)
        return spec.cost(input_tokens, req.max_output_tokens, req.max_output_tokens // 2)

    def _record(self, spec: ModelSpec, usage: Usage) -> None:
        cost = spec.cost(usage.input_tokens, usage.output_tokens, usage.reasoning_tokens)
        self.budget.record(cost)

    def _escalate(
        self, task: str, route: TaskRoute, req: LLMRequest, errors: list[str]
    ) -> Optional[LLMResponse]:
        """Retry the same request on the reasoning tier, once."""
        deep_ref = self.model_overrides.get("deep", TIER_ALIASES["deep"])
        if resolve_alias(route.primary) == deep_ref:
            return None  # already on the reasoning tier
        spec = resolve(deep_ref)
        provider = self._provider_for(spec)
        if provider is None:
            return None
        try:
            self.budget.check(self._estimate(spec, req))
        except BudgetExceeded:
            errors.append("escalation skipped: budget")
            return None
        compiled = compile_request(
            req,
            spec,
            task="deep_plan",
        )
        try:
            response = provider.complete(req, spec, compiled)
        except ProviderError as exc:
            errors.append(f"escalation {spec.id}: {exc.code}")
            return None
        self._record(spec, response.usage)
        return response
