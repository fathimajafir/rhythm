"""Provider protocol and errors."""
from __future__ import annotations

from typing import Protocol

from ..capabilities import ModelSpec
from ..compiler import Compiled
from ..messages import LLMRequest, LLMResponse


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.provider = provider


class Provider(Protocol):
    """One method, one contract: neutral request in, neutral response out.

    Provider-specific wire details (endpoints, auth, field names) stay inside
    the implementation. The agent never sees them.
    """

    name: str

    def complete(self, req: LLMRequest, spec: ModelSpec, compiled: Compiled) -> LLMResponse: ...
