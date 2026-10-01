"""The budget machinery wired through the real endpoints."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm import LLMClient, get_llm_client
from llm.cache import FileCache
from llm.costlog import CostLog
from tests.factories import LeadFactory
from tests.fakes import ScriptedBackend, ir_completion, text_completion
from tests.nl_helpers import leads_by_stage


@pytest.fixture
def wired(
    client: TestClient, tmp_path: Path, admin_headers: dict[str, str]
) -> Iterator[tuple[TestClient, ScriptedBackend, Path]]:
    backend = ScriptedBackend(
        default=lambda req: (
            ir_completion(leads_by_stage())
            if req.task == "nl_compile"
            else text_completion("Summary.")
        )
    )
    log = tmp_path / "costs.jsonl"
    llm = LLMClient(backend, cache=FileCache(tmp_path / "cache"), cost_log=CostLog(log))
    client.app.dependency_overrides[get_llm_client] = lambda: llm  # type: ignore[attr-defined]
    client.headers.update(admin_headers)
    yield client, backend, log


def test_repeating_a_question_costs_nothing_and_says_so(
    wired: tuple[TestClient, ScriptedBackend, Path],
) -> None:
    client, backend, log = wired
    LeadFactory.create_batch(2)
    first = client.post("/query", json={"question": "leads per stage"}).json()
    second = client.post("/query", json={"question": "leads per stage"}).json()
    assert first["cost_usd"] > 0 and first["cached"] is False
    assert second["cost_usd"] == 0 and second["cached"] is True
    assert second["rows"] == first["rows"] and second["explanation"] == first["explanation"]
    assert len(backend.requests) == 2  # compile + explain, once
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["task"], e["cached"]) for e in lines] == [
        ("nl_compile", False), ("nl_explain", False), ("nl_compile", True), ("nl_explain", True),
    ]  # fmt: skip
    assert sum(e["usd"] for e in lines) == pytest.approx(first["cost_usd"])


def test_a_different_question_misses_the_cache(
    wired: tuple[TestClient, ScriptedBackend, Path],
) -> None:
    client, backend, _ = wired
    client.post("/query", json={"question": "leads per stage"})
    client.post("/query", json={"question": "how many leads by stage?"})
    assert len(backend.requests) == 4


def test_new_data_changes_the_explanation_cache_but_not_the_compile_cache(
    wired: tuple[TestClient, ScriptedBackend, Path],
) -> None:
    client, backend, _ = wired
    LeadFactory()
    client.post("/query", json={"question": "leads per stage"})
    LeadFactory()  # row counts change; the schema (and so the compile cache key) does not
    again = client.post("/query", json={"question": "leads per stage"}).json()
    tasks = [r.task for r in backend.requests]
    assert tasks == ["nl_compile", "nl_explain", "nl_explain"]
    assert again["cached"] is False and again["cost_usd"] > 0


def test_invalid_ir_is_never_cached(wired: tuple[TestClient, ScriptedBackend, Path]) -> None:
    client, backend, _ = wired
    bad = {"tables": ["nope"], "select": [{"agg": "count"}]}
    backend._queue = [ir_completion(bad), ir_completion(bad)]
    assert client.post("/query", json={"question": "something odd"}).status_code == 422
    backend._queue = [ir_completion(leads_by_stage()), text_completion("ok.")]
    ok = client.post("/query", json={"question": "something odd"})
    assert ok.status_code == 200  # the bad answer was not replayed from the cache


def test_budget_exhaustion_is_a_429_with_cost_zero(
    client: TestClient, tmp_path: Path, admin_headers: dict[str, str]
) -> None:
    log = tmp_path / "c.jsonl"
    log.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "usd": 10,
            }
        )
        + "\n"
    )
    llm = LLMClient(ScriptedBackend(), cost_log=CostLog(log, daily_budget_usd=5.0))
    client.app.dependency_overrides[get_llm_client] = lambda: llm  # type: ignore[attr-defined]
    resp = client.post("/query", json={"question": "leads per stage"}, headers=admin_headers)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "budget_exceeded"
    assert resp.json()["cost_usd"] == 0
