import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from evals.compare import rows_equivalent
from evals.eval_queries import (
    DEFAULT_GOLDEN,
    ExpectedRows,
    evaluate_case,
    format_report,
    summarize,
)
from evals.golden import GoldenCase, load_golden
from llm import LLMClient, get_llm_client
from nlquery.canonical import canonicalize
from nlquery.catalog import load_catalog
from nlquery.ir import check_ir, parse_ir
from tests.factories import CompanyFactory, LeadFactory
from tests.fakes import ScriptedBackend, ir_completion, text_completion, unanswerable
from tests.nl_helpers import leads_by_stage, leads_join_companies, ref


def canon(payload: dict[str, Any]) -> dict[str, Any]:
    return canonicalize(parse_ir(payload))


class TestCanonicalize:
    def test_ignores_aliases_and_filter_order(self) -> None:
        a = leads_by_stage() | {
            "filters": [
                {"column": ref("leads", "stage"), "op": "in", "value": ["won", "lost"]},
                {"column": ref("leads", "budget_usd"), "op": "gt", "value": 1000},
            ]
        }
        b = leads_by_stage() | {
            "filters": [
                {"column": ref("leads", "budget_usd"), "op": "gt", "value": 1000.0},
                {"column": ref("leads", "stage"), "op": "in", "value": ["lost", "won"]},
            ],
            "select": [{"column": ref("leads", "stage")}, {"agg": "count", "alias": "n"}],
            "order_by": [{"alias": "n", "direction": "desc"}],
        }
        assert canon(a) == canon(b)

    def test_limit_null_equals_default_100(self) -> None:
        assert canon(leads_by_stage()) == canon(leads_by_stage() | {"limit": 100})
        assert canon(leads_by_stage()) != canon(leads_by_stage() | {"limit": 5})

    def test_join_orientation_and_table_order(self) -> None:
        a = leads_join_companies()
        b = leads_join_companies(
            tables=["companies", "leads"],
            joins=[{"left": ref("companies", "id"), "right": ref("leads", "company_id")}],
        )
        assert canon(a) == canon(b)

    def test_left_join_keeps_base_table_significant(self) -> None:
        a = leads_join_companies(joins=[{**leads_join_companies()["joins"][0], "type": "left"}])
        b = leads_join_companies(
            tables=["companies", "leads"],
            joins=[
                {
                    "left": ref("companies", "id"),
                    "right": ref("leads", "company_id"),
                    "type": "left",
                }
            ],
        )
        assert canon(a) != canon(b)

    def test_order_by_column_equals_order_by_matching_alias(self) -> None:
        listing = {"tables": ["leads"], "select": [{"column": ref("leads", "email")}]}
        a = listing | {"order_by": [{"column": ref("leads", "email")}]}
        b = listing | {"order_by": [{"alias": "email"}]}
        assert canon(a) == canon(b)

    @pytest.mark.parametrize(
        "mutation",
        [
            {"filters": [{"column": ref("leads", "stage"), "op": "neq", "value": "won"}]},
            {"order_by": [{"alias": "lead_count", "direction": "asc"}]},
            {
                "select": [
                    {"agg": "count", "alias": "lead_count"},
                    {"column": ref("leads", "stage")},
                ]
            },
            {"group_by": []},
            {
                "select": [
                    {"column": ref("leads", "source")},
                    {"agg": "count", "alias": "lead_count"},
                ]
            },
        ],
    )
    def test_meaningful_differences_are_detected(self, mutation: dict[str, Any]) -> None:
        other = leads_by_stage() | mutation
        if "select" in mutation and "group_by" not in mutation:
            other["group_by"] = [
                {"column": mutation["select"][0].get("column", ref("leads", "stage"))}
            ]
        assert canon(leads_by_stage()) != canon(other)

    def test_chart_hint_excluded_by_default(self) -> None:
        a, b = leads_by_stage(), leads_by_stage() | {"chart_hint": "pie"}
        assert canon(a) == canon(b)
        assert canonicalize(parse_ir(a), include_chart_hint=True) != canonicalize(
            parse_ir(b), include_chart_hint=True
        )


class TestRowsEquivalent:
    def test_unordered_multiset(self) -> None:
        assert rows_equivalent([["a", 1], ["b", 2]], [["b", 2], ["a", 1]], ordered=False)
        assert not rows_equivalent([["a", 1], ["b", 2]], [["b", 2], ["a", 1]], ordered=True)
        assert rows_equivalent([["a", 1]], [["a", 1]], ordered=True)

    def test_duplicates_matter(self) -> None:
        assert not rows_equivalent([["a"], ["a"]], [["a"], ["b"]], ordered=False)
        assert not rows_equivalent([["a"], ["a"]], [["a"]], ordered=False)

    def test_float_noise_and_int_float(self) -> None:
        assert rows_equivalent([[1, 0.1 + 0.2]], [[1.0, 0.3]], ordered=True)
        assert not rows_equivalent([[1, 0.31]], [[1, 0.3]], ordered=True)

    def test_column_permutation_is_equivalent(self) -> None:
        assert rows_equivalent([[3, "won"], [2, "new"]], [["won", 3], ["new", 2]], ordered=False)
        assert not rows_equivalent([[3, "won"]], [["won", 4]], ordered=False)

    def test_none_and_empty(self) -> None:
        assert rows_equivalent([], [], ordered=False)
        assert rows_equivalent([[None, 1]], [[None, 1]], ordered=True)
        assert not rows_equivalent([[None]], [["None"]], ordered=True)
        assert not rows_equivalent([], [[1]], ordered=False)

    def test_booleans(self) -> None:
        assert rows_equivalent([[True]], [[True]], ordered=True)
        assert not rows_equivalent([[True]], [[False]], ordered=True)


class TestGoldenFile:
    def write(self, tmp_path: Path, *lines: object) -> Path:
        path = tmp_path / "g.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def case(self, **kw: Any) -> dict[str, Any]:
        return {"id": "c1", "question": "q?", "expected_ir": leads_by_stage()} | kw

    def test_valid_file(self, tmp_path: Path) -> None:
        loaded = load_golden(self.write(tmp_path, self.case(), self.case(id="c2")))
        assert [c.id for c in loaded.cases] == ["c1", "c2"] and loaded.errors == []

    def test_blank_lines_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "g.jsonl"
        path.write_text("\n" + json.dumps(self.case()) + "\n\n")
        assert len(load_golden(path).cases) == 1

    @pytest.mark.parametrize(
        "bad",
        [
            {"id": "x", "question": "q"},  # no label
            {"id": "x", "question": "q", "expect_unanswerable": True, "expected_rows": []},
            {"id": "x", "question": "q", "expected_ir": {"tables": []}},
            {"id": "x", "question": "q", "expected_rows": [[1]], "bogus": 1},
        ],
    )
    def test_bad_cases_are_reported_with_line_numbers(
        self, tmp_path: Path, bad: dict[str, Any]
    ) -> None:
        loaded = load_golden(self.write(tmp_path, self.case(), bad))
        assert len(loaded.cases) == 1 and loaded.errors[0].startswith("g.jsonl:2:")

    def test_duplicate_ids_and_bad_json(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, self.case(), self.case())
        path.write_text(path.read_text() + "{not json\n")
        errors = load_golden(path).errors
        assert any("duplicate id" in e for e in errors) and any(":3:" in e for e in errors)

    def test_checked_in_golden_file_is_valid_against_the_live_schema(self, db: Session) -> None:
        """Guards your labels in CI: a typo in an expected_ir fails here, before any model call."""
        loaded = load_golden(DEFAULT_GOLDEN)
        assert loaded.errors == []
        catalog = load_catalog(db)
        for case in loaded.cases:
            if case.expected_ir is not None:
                assert check_ir(case.expected_ir, catalog) == [], case.id


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


@pytest.fixture
def api(
    client: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> Iterator[TestClient]:
    client.app.dependency_overrides[get_llm_client] = lambda: LLMClient(backend)  # type: ignore[attr-defined]
    client.headers.update(admin_headers)
    yield client


def run_case(api: TestClient, db: Session, **case: Any) -> Any:
    golden = GoldenCase.model_validate({"id": "c", "question": "some question"} | case)
    return evaluate_case(golden, api, ExpectedRows(db))  # type: ignore[arg-type]


class TestEvaluateCase:
    def seed(self) -> None:
        LeadFactory.create_batch(2, stage="won")
        LeadFactory(stage="new")

    def test_exact_and_equivalent(
        self, api: TestClient, backend: ScriptedBackend, db: Session
    ) -> None:
        self.seed()
        backend.queue(ir_completion(leads_by_stage()), text_completion("ok."))
        r = run_case(api, db, expected_ir=leads_by_stage())
        assert (r.status, r.exact_match, r.equivalent, r.chart_hint_match) == (
            "pass",
            True,
            True,
            True,
        )
        assert r.cost_usd > 0

    def test_different_ir_same_answer_is_equivalent_not_exact(
        self, api: TestClient, backend: ScriptedBackend, db: Session
    ) -> None:
        self.seed()
        won_only = {
            "tables": ["leads"],
            "select": [{"agg": "count", "alias": "n"}],
            "filters": [{"column": ref("leads", "stage"), "op": "eq", "value": "won"}],
        }
        backend.queue(
            ir_completion(
                won_only
                | {
                    "filters": won_only["filters"]
                    + [{"column": ref("leads", "budget_usd"), "op": "gte", "value": 0}]
                }
            ),
            text_completion("ok."),
        )
        r = run_case(api, db, expected_ir=won_only)
        assert (r.status, r.exact_match, r.equivalent) == ("pass", False, True)
        assert "different IR, same answer" in r.notes

    def test_wrong_rows_fail(self, api: TestClient, backend: ScriptedBackend, db: Session) -> None:
        self.seed()
        wrong = leads_by_stage() | {
            "filters": [{"column": ref("leads", "stage"), "op": "eq", "value": "won"}]
        }
        backend.queue(ir_completion(wrong), text_completion("ok."))
        r = run_case(api, db, expected_ir=leads_by_stage())
        assert (r.status, r.equivalent, r.exact_match) == ("fail", False, False)

    def test_expected_rows_are_authoritative(
        self, api: TestClient, backend: ScriptedBackend, db: Session
    ) -> None:
        self.seed()
        backend.queue(ir_completion(leads_by_stage()), text_completion("ok."))
        r = run_case(api, db, expected_rows=[["won", 2], ["new", 1]], ordered=True)
        assert r.status == "pass" and r.exact_match is None

    def test_unanswerable_expectation(
        self, api: TestClient, backend: ScriptedBackend, db: Session
    ) -> None:
        backend.queue(unanswerable())
        assert run_case(api, db, expect_unanswerable=True).status == "pass"
        backend.queue(ir_completion(leads_by_stage()), text_completion("ok."))
        assert run_case(api, db, expect_unanswerable=True).status == "fail"

    def test_refusal_when_an_answer_was_expected_fails(
        self, api: TestClient, backend: ScriptedBackend, db: Session
    ) -> None:
        backend.queue(unanswerable())
        r = run_case(api, db, expected_ir=leads_by_stage())
        assert r.status == "fail" and "unanswerable" in r.notes[0]

    def test_invalid_label_is_reported_as_golden_invalid_not_model_failure(
        self, api: TestClient, backend: ScriptedBackend, db: Session
    ) -> None:
        self.seed()
        bad_label = leads_by_stage() | {
            "filters": [{"column": ref("leads", "stage"), "op": "eq", "value": "closed"}]
        }
        backend.queue(ir_completion(leads_by_stage()), text_completion("ok."))
        r = run_case(api, db, expected_ir=bad_label)
        assert r.status == "golden_invalid" and "must be one of" in r.notes[0]

    def test_unreachable_api_is_an_error(self, db: Session) -> None:
        import httpx

        golden = GoldenCase.model_validate(
            {"id": "c", "question": "q", "expected_ir": leads_by_stage()}
        )
        with httpx.Client(base_url="http://127.0.0.1:1", timeout=1) as dead:
            r = evaluate_case(golden, dead, ExpectedRows(db))
        assert r.status == "error"


def test_summary_rates_and_report(api: TestClient, backend: ScriptedBackend, db: Session) -> None:
    CompanyFactory()
    LeadFactory(stage="won")
    backend.queue(ir_completion(leads_by_stage()), text_completion("ok."), unanswerable())
    results = [
        run_case(api, db, id="a", expected_ir=leads_by_stage()),
        run_case(api, db, id="b", expect_unanswerable=True),
    ]
    summary = summarize(results)
    assert (summary.total, summary.passed, summary.exact_rate, summary.equivalence_rate) == (
        2,
        2,
        1.0,
        1.0,
    )
    assert summary.unanswerable_rate == 1.0 and summary.total_cost_usd > 0
    assert "exact match        100.0%" in format_report(results, summary)


def test_summary_of_empty_and_all_invalid() -> None:
    assert summarize([]).exact_rate is None


def test_replay_compatible_flag_skips_rows_only_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evals import eval_queries

    path = tmp_path / "g.jsonl"
    cases = [
        {"id": "a", "question": "q a", "expected_ir": leads_by_stage()},
        {"id": "b", "question": "q b", "expected_rows": [[1]]},
        {"id": "c", "question": "q c", "expect_unanswerable": True},
    ]
    path.write_text("\n".join(json.dumps(c) for c in cases))
    seen: list[str] = []

    def fake_run(selected: list[GoldenCase], *_: object) -> list[object]:
        seen.extend(c.id for c in selected)
        raise SystemExit(0)

    monkeypatch.setattr(eval_queries, "run_eval", fake_run)
    monkeypatch.setattr(eval_queries, "login", lambda *_: None)
    monkeypatch.setattr(
        eval_queries, "ExpectedRows", lambda: type("E", (), {"close": lambda s: None})()
    )
    with pytest.raises(SystemExit):
        eval_queries.main(["--golden", str(path), "--replay-compatible", "--no-save"])
    assert seen == ["a", "c"]
    assert "skipping 1 case(s)" in capsys.readouterr().out
