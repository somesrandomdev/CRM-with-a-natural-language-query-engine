"""Offline backend that answers from a golden-cases file instead of calling a model.

It exists so CI (and anyone without an API key) can run the *whole* query pipeline end to end:
`nl_compile` requests are answered with the labelled `expected_ir` for that exact question, and
`nl_explain` with a fixed sentence. This validates everything downstream of the model (IR checks,
builder, validator, executor, API) but, by construction, says nothing about model quality.
"""

import json
from pathlib import Path
from typing import Any, Self

from llm.client import LLMError, LLMRequest, RawCompletion, Usage


class ReplayBackend:
    def __init__(self, ir_by_question: dict[str, dict[str, Any]]) -> None:
        self._ir_by_question = ir_by_question

    @classmethod
    def from_golden_file(cls, path: Path) -> Self:
        mapping: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    case = json.loads(line)
                    if case.get("expected_ir") is not None:
                        mapping[case["question"].strip()] = case["expected_ir"]
        return cls(mapping)

    def complete(self, request: LLMRequest) -> RawCompletion:
        usage = Usage(
            input_tokens=(len(request.system) + len(request.user)) // 4, output_tokens=120
        )
        if request.task == "nl_compile":
            ir = self._ir_by_question.get(request.user.strip())
            if ir is None:
                payload: dict[str, Any] = {
                    "unanswerable_reason": "No replay fixture for this question."
                }
            else:
                payload = {"ir": ir}
            return RawCompletion(text=None, tool_input=payload, usage=usage)
        if request.task == "nl_explain":
            return RawCompletion(
                text="Replay backend: no model explanation is available.",
                tool_input=None,
                usage=usage,
            )
        raise LLMError(f"replay backend has no fixture for task {request.task!r}")
