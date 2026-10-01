"""Plain data types shared by the LLM client, cache, cost log and backends."""

from dataclasses import dataclass, field
from typing import Any, Protocol


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
