"""Loading and validating the golden case file (JSONL, one case per line)."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from nlquery.ir import QueryIR


class GoldenCase(BaseModel):
    """One labelled question.

    Label at least one of:
      * `expected_ir`   - the IR a correct compiler should produce (enables exact-match and lets the
                          runner derive the expected rows by executing it), and/or
      * `expected_rows` - the exact rows (list of lists, in select order) a correct answer returns
                          (authoritative for result-equivalence when present).
    For questions the system should refuse, set `expect_unanswerable: true` instead.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    expected_ir: QueryIR | None = None
    expected_rows: list[list[Any]] | None = None
    expect_unanswerable: bool = False
    # Compare rows in order. Defaults to true when the expected IR has an order_by.
    ordered: bool | None = None
    tags: list[str] = []
    notes: str | None = None

    @model_validator(mode="after")
    def _has_a_label(self) -> "GoldenCase":
        if self.expect_unanswerable:
            if self.expected_ir is not None or self.expected_rows is not None:
                raise ValueError("expect_unanswerable cannot be combined with expected_ir/rows")
        elif self.expected_ir is None and self.expected_rows is None:
            raise ValueError(
                "label at least one of expected_ir, expected_rows, expect_unanswerable"
            )
        return self


@dataclass(frozen=True)
class LoadedGolden:
    cases: list[GoldenCase]
    errors: list[str] = field(default_factory=list)


def load_golden(path: Path) -> LoadedGolden:
    cases: list[GoldenCase] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = GoldenCase.model_validate(json.loads(line))
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}:{lineno}: {exc}")
            continue
        if case.id in seen:
            errors.append(f"{path.name}:{lineno}: duplicate id {case.id!r}")
            continue
        seen.add(case.id)
        cases.append(case)
    return LoadedGolden(cases, errors)
