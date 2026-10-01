"""One LLM call that turns (IR + result sample) into a 1-2 sentence plain-English summary.

The explanation is decoration, never a dependency: if the call fails or returns something
unusable, a deterministic description built from the IR is returned instead.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from llm import LLMClient, LLMError, LLMRequest
from llm.client import SONNET
from nlquery.ir import Filter, QueryIR, RelativeWindow, SelectItem
from nlquery.prompts import EXPLAINER_PROMPT_PATH, Prompt, load_prompt

SAMPLE_ROWS = 10
MAX_CELL_CHARS = 120
MAX_EXPLANATION_CHARS = 600


@dataclass(frozen=True)
class Explanation:
    text: str
    source: Literal["llm", "fallback"]
    usd: float = 0.0
    cached: bool = False


def _trim(value: object) -> object:
    return value[:MAX_CELL_CHARS] if isinstance(value, str) else value


def explain(
    llm: LLMClient,
    *,
    question: str,
    ir: QueryIR,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
    schema_hash: str,
    prompt: Prompt | None = None,
    model: str = SONNET,
) -> Explanation:
    prompt = prompt or load_prompt(EXPLAINER_PROMPT_PATH)
    payload = {
        "question": question,
        "query": ir.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        "columns": columns,
        "rows_returned": len(rows),
        "truncated": truncated,
        "sample_rows": [[_trim(v) for v in r] for r in rows[:SAMPLE_ROWS]],
    }
    try:
        result = llm.complete(
            LLMRequest(
                task="nl_explain",
                model=model,
                system=prompt.body,
                user="<data>\n" + json.dumps(payload, default=str) + "\n</data>",
                max_tokens=300,
                prompt_version=prompt.version,
                schema_hash=schema_hash,
            ),
            cacheable=lambda raw: _usable(raw.text),
        )
    except LLMError:
        return Explanation(describe(ir, len(rows), truncated), "fallback")
    text = " ".join((result.text or "").split())
    if not _usable(text):
        return Explanation(
            describe(ir, len(rows), truncated), "fallback", result.usd, result.cached
        )
    return Explanation(text, "llm", result.usd, result.cached)


def _usable(text: str | None) -> bool:
    cleaned = " ".join((text or "").split())
    return bool(cleaned) and len(cleaned) <= MAX_EXPLANATION_CHARS


# ---------------------------------------------------------------- deterministic fallback


def _describe_filter(f: Filter) -> str:
    name = f"{f.column.table}.{f.column.column}"
    v = f.value
    if isinstance(v, RelativeWindow):
        return f"{name} in the last {v.amount} {v.unit}(s)"
    words = {
        "eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
        "in": "in", "not_in": "not in", "contains": "contains", "starts_with": "starts with",
        "between": "between", "is_null": "is empty", "is_not_null": "is not empty",
        "in_period": "in", "in_last": "in the last",
    }  # fmt: skip
    suffix = "" if v is None else f" {v}"
    return f"{name} {words[f.op]}{suffix}"


def _describe_item(item: SelectItem) -> str:
    if item.agg and item.column is None:
        return "the number of rows"
    target = f"{item.column.table}.{item.column.column}" if item.column else "value"
    if item.agg:
        return f"the {item.agg.replace('_', ' ')} of {target}"
    return target if item.bucket is None else f"{target} by {item.bucket}"


def describe(ir: QueryIR, row_count: int, truncated: bool = False) -> str:
    shown = ", ".join(_describe_item(i) for i in ir.select)
    text = f"Showing {shown} from {', '.join(ir.tables)}"
    if ir.filters:
        text += " where " + " and ".join(_describe_filter(f) for f in ir.filters)
    text += f" ({row_count} row{'s' if row_count != 1 else ''}"
    text += ", limited to the first 100)." if truncated else ")."
    return text
