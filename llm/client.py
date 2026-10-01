"""LLM client wrapper.

Every model call in the codebase goes through `LLMClient.complete`. The wrapper owns the model
allow-list (nothing outside it, in particular no Opus model, can be called), computes the
USD cost of each call from token usage, and delegates the network call to a pluggable backend so
tests and CI can run with no API key.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import anthropic

from app.config import get_settings

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


class LLMError(Exception):
    """A model call failed or returned something unusable."""


class ModelNotAllowedError(LLMError):
    """The requested model is not on the allow-list."""


@dataclass(frozen=True)
class ToolSpec:
    """A single forced tool call, used to get schema-constrained JSON back from the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMRequest:
    task: str  # e.g. "nl_compile": which feature is calling, for cost attribution
    model: str
    system: str
    user: str
    max_tokens: int
    prompt_version: str
    schema_hash: str = ""
    tool: ToolSpec | None = None


@dataclass(frozen=True)
class RawCompletion:
    text: str | None
    tool_input: dict[str, Any] | None
    usage: Usage


@dataclass(frozen=True)
class LLMResult:
    text: str | None
    tool_input: dict[str, Any] | None
    usage: Usage
    usd: float
    model: str
    cached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class LLMBackend(Protocol):
    def complete(self, request: LLMRequest) -> RawCompletion: ...


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


class LLMClient:
    def __init__(self, backend: LLMBackend) -> None:
        self._backend = backend

    def complete(self, request: LLMRequest) -> LLMResult:
        if request.model not in ALLOWED_MODELS:
            raise ModelNotAllowedError(
                f"Model {request.model!r} is not on the allow-list {sorted(ALLOWED_MODELS)}"
            )
        raw = self._backend.complete(request)
        return LLMResult(
            text=raw.text,
            tool_input=raw.tool_input,
            usage=raw.usage,
            usd=compute_usd(request.model, raw.usage),
            model=request.model,
        )


@lru_cache
def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.llm_backend == "replay":
        from llm.replay import ReplayBackend

        return LLMClient(ReplayBackend.from_golden_file(settings.llm_replay_file))
    if settings.anthropic_api_key is None:
        raise LLMError("ANTHROPIC_API_KEY is not configured")
    return LLMClient(AnthropicBackend(settings.anthropic_api_key.get_secret_value()))
