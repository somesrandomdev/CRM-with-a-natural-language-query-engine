import json

import pytest
from sqlalchemy.orm import Session

from llm.client import SONNET
from nlquery.catalog import Catalog, load_catalog
from nlquery.compiler import CompileError, QueryCompiler
from tests.fakes import ir_completion, make_client, text_completion, unanswerable
from tests.nl_helpers import leads_by_stage, ref


@pytest.fixture
def catalog(db: Session) -> Catalog:
    return load_catalog(db)


def test_happy_path_returns_ir_and_cost(catalog: Catalog) -> None:
    llm, backend = make_client(ir_completion(leads_by_stage()))
    result = QueryCompiler(llm).compile("  leads per stage?  ", catalog)
    assert result.ir is not None and result.ir.tables == ["leads"]
    assert result.attempts == 1
    assert result.usd == pytest.approx(0.006)
    (req,) = backend.requests
    assert req.user == "leads per stage?"  # the question verbatim, no scaffolding
    assert req.model == SONNET
    assert req.tool is not None and req.tool.name == "emit_query_ir"
    assert req.schema_hash == catalog.schema_hash


def test_prompt_contains_live_schema_and_no_secrets(catalog: Catalog) -> None:
    llm, backend = make_client(ir_completion(leads_by_stage()))
    QueryCompiler(llm).compile("anything", catalog)
    system = backend.requests[0].system
    assert "TABLE leads" in system and "stage: enum, one of: new | qualified" in system
    assert "FOREIGN KEYS" in system
    assert "hashed_password" not in system
    assert "{{" not in system


def test_tool_schema_has_no_free_text_sql_field() -> None:
    from nlquery.compiler import TOOL

    schema = json.dumps(TOOL.input_schema).lower()
    assert '"sql"' not in schema and "raw" not in schema
    assert TOOL.input_schema["additionalProperties"] is False


def test_unanswerable_is_returned_not_raised(catalog: Catalog) -> None:
    llm, _ = make_client(unanswerable("Nothing about weather."))
    result = QueryCompiler(llm).compile("weather in Paris?", catalog)
    assert result.ir is None and result.unanswerable_reason == "Nothing about weather."


def test_invalid_ir_triggers_one_repair_with_validator_feedback(catalog: Catalog) -> None:
    bad = leads_by_stage() | {
        "filters": [{"column": ref("leads", "stage"), "op": "eq", "value": "closed"}]
    }
    llm, backend = make_client(ir_completion(bad), ir_completion(leads_by_stage()))
    result = QueryCompiler(llm).compile("closed leads by stage", catalog)
    assert result.attempts == 2
    assert result.usd == pytest.approx(0.012)  # both attempts are billed
    assert result.history and "must be one of" in result.history[0][0]
    repair = backend.requests[1].user
    assert repair.startswith("closed leads by stage")
    assert "must be one of" in repair and '"closed"' in repair


def test_schema_violation_is_repaired_too(catalog: Catalog) -> None:
    bad = {"tables": ["leads"], "select": [], "sql": "DROP TABLE leads"}
    llm, backend = make_client(ir_completion(bad), ir_completion(leads_by_stage()))
    result = QueryCompiler(llm).compile("x?", catalog)
    assert result.attempts == 2
    assert "sql" in backend.requests[1].user  # told the extra field was rejected


def test_gives_up_after_repair_budget_and_reports_cost(catalog: Catalog) -> None:
    bad = {"tables": ["nope"], "select": [{"agg": "count"}]}
    llm, backend = make_client(ir_completion(bad), ir_completion(bad), ir_completion(bad))
    with pytest.raises(CompileError) as info:
        QueryCompiler(llm).compile("q?", catalog)
    assert len(backend.requests) == 2  # 1 attempt + 1 repair, no more
    assert info.value.usd == pytest.approx(0.012)
    assert any("unknown table" in e for e in info.value.errors)


def test_model_returning_neither_ir_nor_reason_is_rejected(catalog: Catalog) -> None:
    llm, _ = make_client(text_completion("SELECT * FROM leads"), text_completion("SELECT 1"))
    with pytest.raises(CompileError):
        QueryCompiler(llm).compile("q?", catalog)


def test_model_cannot_smuggle_sql_through_an_alias_or_value(catalog: Catalog) -> None:
    sneaky = leads_by_stage()
    sneaky["select"][1]["alias"] = "n; DROP TABLE leads"
    llm, _ = make_client(ir_completion(sneaky), ir_completion(sneaky))
    with pytest.raises(CompileError) as info:
        QueryCompiler(llm).compile("q?", catalog)
    assert any("alias" in e for e in info.value.errors)


@pytest.mark.parametrize("question", ["", "   ", "x" * 501])
def test_question_length_is_enforced_before_any_llm_call(question: str, catalog: Catalog) -> None:
    llm, backend = make_client()
    with pytest.raises(CompileError):
        QueryCompiler(llm).compile(question, catalog)
    assert backend.requests == []
