from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import LeadStage, User
from llm import LLMClient, LLMError, get_llm_client
from nlquery.executor import QueryTimeoutError
from tests.factories import CompanyFactory, LeadFactory, UserFactory
from tests.fakes import ScriptedBackend, ir_completion, text_completion, unanswerable
from tests.nl_helpers import leads_by_stage, ref


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


@pytest.fixture
def api(client: TestClient, backend: ScriptedBackend) -> Iterator[TestClient]:
    client.app.dependency_overrides[get_llm_client] = lambda: LLMClient(backend)  # type: ignore[attr-defined]
    yield client


def ask(api: TestClient, headers: dict[str, str], question: str = "leads per stage") -> Any:
    return api.post("/query", json={"question": question}, headers=headers)


def test_requires_authentication(api: TestClient) -> None:
    assert api.post("/query", json={"question": "anything at all"}).status_code == 401


def test_success_response_shape(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    LeadFactory.create_batch(2, stage=LeadStage.won)
    LeadFactory(stage=LeadStage.new)
    backend.queue(ir_completion(leads_by_stage()), text_completion("Two leads are won."))
    resp = ask(api, admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["columns"] == ["stage", "lead_count"]
    assert body["rows"] == [["won", 2], ["new", 1]]
    assert body["row_count"] == 2 and body["truncated"] is False
    assert body["chart_hint"] == "bar"
    assert body["explanation"] == "Two leads are won." and body["explanation_source"] == "llm"
    assert body["sql"].startswith("SELECT leads.stage") and body["sql"].endswith("LIMIT 100")
    assert body["ir"]["tables"] == ["leads"]
    assert body["cost_usd"] == pytest.approx(0.006 + 0.0015, abs=1e-9)


def test_cost_is_reported_on_failures_too(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    backend.queue(unanswerable("No weather data."))
    resp = ask(api, admin_headers, "what's the weather")
    assert resp.status_code == 422
    assert resp.json()["error"] == {
        "code": "unanswerable",
        "message": "No weather data.",
        "details": [],
    }
    assert resp.json()["cost_usd"] > 0


def test_invalid_ir_after_repair_is_a_422_with_details(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    bad = {"tables": ["nope"], "select": [{"agg": "count"}]}
    backend.queue(ir_completion(bad), ir_completion(bad))
    resp = ask(api, admin_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_ir"
    assert any("unknown table" in d for d in resp.json()["error"]["details"])
    assert resp.json()["cost_usd"] == pytest.approx(0.012)


def test_rep_only_sees_own_leads_through_the_query_path(
    api: TestClient, backend: ScriptedBackend, rep: User, rep_headers: dict[str, str]
) -> None:
    LeadFactory.create_batch(2, owner=rep, stage=LeadStage.won)
    LeadFactory.create_batch(5, owner=UserFactory(), stage=LeadStage.won)
    backend.queue(ir_completion(leads_by_stage()), text_completion("ok."))
    body = ask(api, rep_headers).json()
    assert body["rows"] == [["won", 2]]
    assert f"owner_id = {rep.id}" in body["sql"]


def test_results_are_truncated_at_100_rows(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    company = CompanyFactory()
    for _ in range(105):
        LeadFactory(
            company=company, owner=UserFactory.build(id=None) if False else None
        ) if False else None
    LeadFactory.create_batch(105, company=company)
    listing = {"tables": ["leads"], "select": [{"column": ref("leads", "email")}]}
    backend.queue(ir_completion(listing), text_completion("Many leads."))
    body = ask(api, admin_headers, "list all lead emails").json()
    assert body["row_count"] == 100 and body["truncated"] is True


def test_explicit_limit_is_not_reported_as_truncation(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    LeadFactory.create_batch(5)
    listing = {"tables": ["leads"], "select": [{"column": ref("leads", "email")}], "limit": 3}
    backend.queue(ir_completion(listing), text_completion("Three leads."))
    body = ask(api, admin_headers, "three lead emails").json()
    assert (body["row_count"], body["truncated"]) == (3, False)


def test_explainer_failure_degrades_gracefully(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    LeadFactory()
    backend.queue(ir_completion(leads_by_stage()), LLMError("overloaded"))
    resp = ask(api, admin_headers)
    assert resp.status_code == 200
    assert resp.json()["explanation_source"] == "fallback"


def test_compile_llm_failure_is_a_502(
    api: TestClient, backend: ScriptedBackend, admin_headers: dict[str, str]
) -> None:
    backend.queue(LLMError("down"))
    resp = ask(api, admin_headers)
    assert resp.status_code == 502 and resp.json()["cost_usd"] == 0.0


def test_timeout_is_a_408_that_still_reports_cost(
    api: TestClient,
    backend: ScriptedBackend,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(*_: Any, **__: Any) -> None:
        raise QueryTimeoutError("query exceeded 5s")

    monkeypatch.setattr("nlquery.service.execute_readonly", slow)
    backend.queue(ir_completion(leads_by_stage()))
    resp = ask(api, admin_headers)
    assert resp.status_code == 408
    assert resp.json()["error"]["code"] == "timeout" and resp.json()["cost_usd"] > 0


def test_a_builder_bug_is_caught_by_the_validator(
    api: TestClient,
    backend: ScriptedBackend,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
) -> None:
    monkeypatch.setattr("nlquery.service.build_sql", lambda *a, **k: "DELETE FROM leads")
    LeadFactory()
    backend.queue(ir_completion(leads_by_stage()))
    resp = ask(api, admin_headers)
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "internal_error"
    assert "DELETE" not in resp.text


@pytest.mark.parametrize("question", ["", "ab", "x" * 501])
def test_question_length_validation(
    api: TestClient, admin_headers: dict[str, str], question: str
) -> None:
    assert ask(api, admin_headers, question).status_code == 422
