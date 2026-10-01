"""Test doubles for the LLM layer."""

from collections.abc import Callable
from typing import Any

from llm import LLMClient
from llm.client import LLMError, LLMRequest, RawCompletion, Usage

Response = RawCompletion | Exception | Callable[[LLMRequest], RawCompletion]


class ScriptedBackend:
    """Returns queued responses in order and records every request it receives."""

    def __init__(self, *responses: Response, default: Response | None = None) -> None:
        self._queue = list(responses)
        self._default = default
        self.requests: list[LLMRequest] = []

    def queue(self, *responses: Response) -> None:
        self._queue.extend(responses)

    def complete(self, request: LLMRequest) -> RawCompletion:
        self.requests.append(request)
        item = self._queue.pop(0) if self._queue else self._default
        if item is None:
            raise AssertionError(f"unexpected LLM call #{len(self.requests)} ({request.task})")
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item


def ir_completion(ir: dict[str, Any], tokens: tuple[int, int] = (1000, 200)) -> RawCompletion:
    return RawCompletion(text=None, tool_input={"ir": ir}, usage=Usage(*tokens))


def unanswerable(reason: str = "Not about CRM data.") -> RawCompletion:
    return RawCompletion(
        text=None, tool_input={"unanswerable_reason": reason}, usage=Usage(500, 30)
    )


def text_completion(text: str, tokens: tuple[int, int] = (300, 40)) -> RawCompletion:
    return RawCompletion(text=text, tool_input=None, usage=Usage(*tokens))


def make_client(
    *responses: Response, default: Response | None = None
) -> tuple[LLMClient, ScriptedBackend]:
    backend = ScriptedBackend(*responses, default=default)
    return LLMClient(backend), backend


__all__ = [
    "LLMError",
    "ScriptedBackend",
    "ir_completion",
    "make_client",
    "text_completion",
    "unanswerable",
]
