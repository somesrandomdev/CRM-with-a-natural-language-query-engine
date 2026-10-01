"""Natural language -> IR (the only place the LLM touches the query path).

The model is forced to answer through a single tool whose input schema is `CompilerOutput`,
so its output is JSON conforming to the IR vocabulary: there is nowhere to put SQL. The IR is
then validated against the live catalog; on failure the validator's messages are fed back for a
bounded number of repair attempts.
"""

import json
from dataclasses import dataclass, field

from llm import LLMClient, LLMRequest, ToolSpec
from llm.client import SONNET
from nlquery.catalog import Catalog
from nlquery.ir import (
    CompilerOutput,
    IRValidationError,
    QueryIR,
    check_ir,
    parse_ir,
)
from nlquery.prompts import COMPILER_PROMPT_PATH, Prompt, load_prompt

TOOL = ToolSpec(
    name="emit_query_ir",
    description="Return the structured query for the question, or why it cannot be answered.",
    input_schema=CompilerOutput.model_json_schema(),
)
MAX_QUESTION_CHARS = 500


class CompileError(Exception):
    """The model could not produce a valid IR within the repair budget."""

    def __init__(self, errors: list[str], usd: float) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.usd = usd


@dataclass(frozen=True)
class CompileResult:
    ir: QueryIR | None
    unanswerable_reason: str | None
    usd: float
    attempts: int
    cached: bool = False  # every model call behind this result was served from the cache
    history: list[list[str]] = field(default_factory=list)  # errors from rejected attempts


class QueryCompiler:
    def __init__(
        self,
        llm: LLMClient,
        prompt: Prompt | None = None,
        model: str = SONNET,
        max_repairs: int = 1,
    ) -> None:
        self._llm = llm
        self._prompt = prompt or load_prompt(COMPILER_PROMPT_PATH)
        self._model = model
        self._max_repairs = max_repairs

    @property
    def prompt_version(self) -> str:
        return self._prompt.version

    def compile(self, question: str, catalog: Catalog) -> CompileResult:
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            raise CompileError([f"question must be 1-{MAX_QUESTION_CHARS} characters"], 0.0)

        system = self._prompt.render(schema=catalog.render_for_prompt())
        user = question
        total_usd = 0.0
        all_cached = True
        history: list[list[str]] = []
        for attempt in range(1, self._max_repairs + 2):
            result = self._llm.complete(
                LLMRequest(
                    task="nl_compile",
                    model=self._model,
                    system=system,
                    user=user,
                    max_tokens=2048,
                    prompt_version=self._prompt.version,
                    schema_hash=catalog.schema_hash,
                    tool=TOOL,
                ),
                # Only valid IRs are cached: a rejected answer must be regenerated, not replayed.
                cacheable=lambda raw: not self._validate(raw.tool_input or {}, catalog)[0],
            )
            total_usd += result.usd
            all_cached &= result.cached
            errors, output = self._validate(result.tool_input or {}, catalog)
            if not errors and output is not None:
                return CompileResult(
                    output.ir,
                    output.unanswerable_reason,
                    total_usd,
                    attempt,
                    cached=all_cached,
                    history=history,
                )
            history.append(errors)
            user = _repair_message(question, result.tool_input, errors)
        raise CompileError(history[-1], total_usd)

    @staticmethod
    def _validate(
        payload: dict[str, object], catalog: Catalog
    ) -> tuple[list[str], CompilerOutput | None]:
        try:
            ir_payload = payload.get("ir")
            if ir_payload is not None:
                parse_ir(ir_payload)  # type: ignore[arg-type]  # readable errors for the repair prompt
            output = CompilerOutput.model_validate(payload)
        except IRValidationError as exc:
            return exc.errors, None
        except ValueError as exc:
            return [str(exc)], None
        if output.ir is not None:
            errors = check_ir(output.ir, catalog)
            if errors:
                return errors, None
        return [], output


def _repair_message(question: str, previous: dict[str, object] | None, errors: list[str]) -> str:
    return (
        f"{question}\n\n---\n"
        "Your previous answer was rejected by the validator and must be corrected.\n"
        f"Previous answer:\n{json.dumps(previous, indent=1, default=str)}\n"
        "Problems:\n" + "\n".join(f"- {e}" for e in errors) + "\n"
        "Return a corrected answer using the tool."
    )
