"""LLM client wrapper.

Every model call in the codebase goes through `LLMClient.complete`. The wrapper owns the model
allow-list (nothing outside it, in particular no Opus model, can be called), computes the
USD cost of each call from token usage, and delegates the network call to a pluggable backend so
tests and CI can run with no API key.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anthropic

from app.config import get_settings
from llm.cache import FileCache, ResponseCache, cache_key
from llm.costlog import CostEntry, CostLog
from llm.types import (
    LLMBackend,
    LLMError,
    LLMRequest,
    LLMResult,
    ModelNotAllowedError,
    RawCompletion,
    ToolSpec,
    Usage,
)

SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input: float
    output: float


# The allow-list *is* the budget policy: a model that is not listed here cannot be called.
ALLOWED_MODELS: Mapping[str, Pricing] = {
    SONNET: Pricing(input=3.0, output=15.0),
    HAIKU: Pricing(input=1.0, output=5.0),
}


class AnthropicBackend:
    """Calls the Anthropic Messages API."""

    def __init__(self, api_key: str, timeout_s: float = 60.0) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    def complete(self, request: LLMRequest) -> RawCompletion:
        kwargs: dict[str, Any] = {}
        if request.tool is not None:
            kwargs["tools"] = [
                {
                    "name": request.tool.name,
                    "description": request.tool.description,
                    "input_schema": request.tool.input_schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": request.tool.name}
        try:
            response = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                **kwargs,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

        usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "max_tokens":
            raise LLMError("Model output was truncated (max_tokens reached)")
        tool_input: dict[str, Any] | None = None
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                tool_input = dict(block.input) if isinstance(block.input, dict) else None
            elif block.type == "text":
                text_parts.append(block.text)
        if request.tool is not None and tool_input is None:
            raise LLMError("Model did not return the expected tool call")
        return RawCompletion(text="".join(text_parts) or None, tool_input=tool_input, usage=usage)


def compute_usd(model: str, usage: Usage) -> float:
    price = ALLOWED_MODELS[model]
    return round(
        usage.input_tokens * price.input / 1_000_000
        + usage.output_tokens * price.output / 1_000_000,
        6,
    )


# Substrings that make a model name unusable no matter what the allow-list says: a second lock,
# so adding an Opus model to ALLOWED_MODELS by mistake still cannot spend Opus money.
FORBIDDEN_MODEL_SUBSTRINGS = ("opus",)


def assert_model_allowed(model: str) -> None:
    lowered = model.lower()
    if any(bad in lowered for bad in FORBIDDEN_MODEL_SUBSTRINGS) or model not in ALLOWED_MODELS:
        raise ModelNotAllowedError(
            f"Model {model!r} is not allowed; permitted models: {sorted(ALLOWED_MODELS)}"
        )


class LLMClient:
    """The only way application code talks to a model.

    Per call it (1) refuses non-allow-listed models, (2) enforces the daily budget if configured,
    (3) serves from the response cache when possible, (4) otherwise calls the backend, and
    (5) appends {model, tokens, usd} to the cost log either way.
    """

    def __init__(
        self,
        backend: LLMBackend,
        *,
        cache: ResponseCache | None = None,
        cost_log: CostLog | None = None,
    ) -> None:
        self._backend = backend
        self._cache = cache
        self._cost_log = cost_log

    def complete(
        self,
        request: LLMRequest,
        *,
        cacheable: Callable[[RawCompletion], bool] | None = None,
    ) -> LLMResult:
        """Run `request`. `cacheable` lets the caller veto caching of a response it will reject
        (e.g. an IR that fails validation), so bad output is never replayed from the cache."""
        assert_model_allowed(request.model)
        key = cache_key(request) if self._cache is not None else None

        if self._cache is not None and key is not None:
            hit = self._cache.get(key)
            if hit is not None:
                saved = compute_usd(request.model, hit.original_usage)
                self._record(request, Usage(), 0.0, cached=True, saved=saved)
                return LLMResult(
                    text=hit.completion.text,
                    tool_input=hit.completion.tool_input,
                    usage=Usage(),
                    usd=0.0,
                    model=request.model,
                    cached=True,
                )

        if self._cost_log is not None:
            self._cost_log.check_budget()
        raw = self._backend.complete(request)
        usd = compute_usd(request.model, raw.usage)
        self._record(request, raw.usage, usd, cached=False)
        if (
            self._cache is not None
            and key is not None
            and (raw.text or raw.tool_input)
            and (cacheable is None or cacheable(raw))
        ):
            self._cache.put(key, raw)
        return LLMResult(
            text=raw.text, tool_input=raw.tool_input, usage=raw.usage, usd=usd, model=request.model
        )

    def _record(
        self, request: LLMRequest, usage: Usage, usd: float, *, cached: bool, saved: float = 0.0
    ) -> None:
        if self._cost_log is not None:
            self._cost_log.record(
                CostEntry(
                    task=request.task,
                    model=request.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    usd=usd,
                    cached=cached,
                    prompt_version=request.prompt_version,
                    saved_usd=saved,
                )
            )


@lru_cache
def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.llm_backend == "replay":
        from llm.replay import ReplayBackend

        # Replayed answers are not model output: never cache them, never log their (fake) cost.
        return LLMClient(ReplayBackend.from_golden_file(settings.llm_replay_file))
    if settings.anthropic_api_key is None:
        raise LLMError("ANTHROPIC_API_KEY is not configured")
    return LLMClient(
        AnthropicBackend(settings.anthropic_api_key.get_secret_value()),
        cache=FileCache(settings.llm_cache_dir) if settings.llm_cache_enabled else None,
        cost_log=CostLog(settings.llm_cost_log, settings.llm_daily_budget_usd),
    )


__all__ = [
    "ALLOWED_MODELS",
    "HAIKU",
    "SONNET",
    "AnthropicBackend",
    "LLMBackend",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResult",
    "ModelNotAllowedError",
    "RawCompletion",
    "ToolSpec",
    "Usage",
    "compute_usd",
    "get_llm_client",
]
