"""Hard budget rules: one client wrapper, a cost log, a response cache, no Opus."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

import llm.client as client_mod
from llm import BudgetExceededError, LLMClient, LLMRequest, ModelNotAllowedError, ToolSpec
from llm.cache import FileCache, MemoryCache, cache_key
from llm.client import HAIKU, SONNET
from llm.costlog import CostLog
from llm.types import RawCompletion, Usage
from tests.fakes import ScriptedBackend, text_completion

REPO = Path(__file__).resolve().parent.parent


def req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "task": "nl_explain", "model": SONNET, "system": "sys", "user": "hello",
        "max_tokens": 100, "prompt_version": "1.0.0", "schema_hash": "abc",
    }  # fmt: skip
    return LLMRequest(**(base | kw))


def entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestCostLog:
    def test_every_call_is_logged_with_model_tokens_and_usd(self, tmp_path: Path) -> None:
        log = tmp_path / "costs.jsonl"
        llm = LLMClient(ScriptedBackend(text_completion("a", (1000, 100))), cost_log=CostLog(log))
        llm.complete(req())
        (entry,) = entries(log)
        assert entry["model"] == SONNET and entry["task"] == "nl_explain"
        assert (entry["input_tokens"], entry["output_tokens"]) == (1000, 100)
        assert entry["usd"] == pytest.approx(0.0045) and entry["cached"] is False
        assert entry["ts"].endswith("+00:00") and entry["prompt_version"] == "1.0.0"

    def test_prompts_and_responses_are_never_written(self, tmp_path: Path) -> None:
        log = tmp_path / "costs.jsonl"
        llm = LLMClient(ScriptedBackend(text_completion("SECRET-RESPONSE")), cost_log=CostLog(log))
        llm.complete(req(user="SECRET-QUESTION", system="SECRET-SYSTEM"))
        raw = log.read_text()
        assert "SECRET" not in raw

    def test_appends_across_calls_and_instances(self, tmp_path: Path) -> None:
        log = tmp_path / "costs.jsonl"
        for model in (SONNET, HAIKU, SONNET):
            LLMClient(ScriptedBackend(text_completion("x")), cost_log=CostLog(log)).complete(
                req(model=model, user=model)
            )
        assert [e["model"] for e in entries(log)] == [SONNET, HAIKU, SONNET]

    def test_unwritable_log_does_not_fail_the_request(self, tmp_path: Path) -> None:
        bad = CostLog(tmp_path / "missing-dir" / "costs.jsonl")
        out = LLMClient(ScriptedBackend(text_completion("ok")), cost_log=bad).complete(req())
        assert out.text == "ok" and out.usd > 0


class TestDailyBudget:
    def test_blocks_calls_once_the_cap_is_reached(self, tmp_path: Path) -> None:
        backend = ScriptedBackend(default=text_completion("x", (1_000_000, 0)))  # $3 per call
        llm = LLMClient(backend, cost_log=CostLog(tmp_path / "c.jsonl", daily_budget_usd=5.0))
        llm.complete(req(user="1"))
        llm.complete(req(user="2"))  # $6 spent: the cap is checked before each call
        with pytest.raises(BudgetExceededError, match="daily LLM budget"):
            llm.complete(req(user="3"))
        assert len(backend.requests) == 2

    def test_spend_is_restored_from_the_log_on_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "c.jsonl"
        backend = ScriptedBackend(default=text_completion("x", (1_000_000, 0)))
        LLMClient(backend, cost_log=CostLog(path, 5.0)).complete(req(user="1"))
        LLMClient(backend, cost_log=CostLog(path, 5.0)).complete(req(user="2"))
        with pytest.raises(BudgetExceededError):
            LLMClient(backend, cost_log=CostLog(path, 5.0)).complete(req(user="3"))

    def test_yesterdays_spend_does_not_count(self, tmp_path: Path) -> None:
        path = tmp_path / "c.jsonl"
        path.write_text(
            json.dumps({"ts": "2020-01-01T00:00:00+00:00", "usd": 999}) + "\nnot json\n"
        )
        LLMClient(ScriptedBackend(text_completion("x")), cost_log=CostLog(path, 1.0)).complete(
            req()
        )

    def test_cache_hits_are_free_and_allowed_when_over_budget(self, tmp_path: Path) -> None:
        cache = MemoryCache()
        backend = ScriptedBackend(default=text_completion("x", (1_000_000, 0)))
        llm = LLMClient(backend, cache=cache, cost_log=CostLog(tmp_path / "c.jsonl", 1.0))
        llm.complete(req())
        with pytest.raises(BudgetExceededError):
            llm.complete(req(user="other"))
        assert llm.complete(req()).cached  # same request still served

    def test_no_cap_by_default(self, tmp_path: Path) -> None:
        backend = ScriptedBackend(default=text_completion("x", (10_000_000, 0)))
        llm = LLMClient(backend, cost_log=CostLog(tmp_path / "c.jsonl"))
        for i in range(3):
            llm.complete(req(user=str(i)))


class TestCache:
    def test_key_is_sha256_of_schema_hash_prompt_version_and_input(self) -> None:
        r = req()
        blob = json.dumps(
            {"task": "nl_explain", "model": SONNET, "user": "hello", "tool": None},
            sort_keys=True, separators=(",", ":"),
        )  # fmt: skip
        expected = hashlib.sha256("\x1f".join(["abc", "1.0.0", blob]).encode()).hexdigest()
        assert cache_key(r) == expected

    @pytest.mark.parametrize(
        "change",
        [{"schema_hash": "def"}, {"prompt_version": "1.0.1"}, {"user": "hello!"},
         {"model": HAIKU}, {"task": "other"}, {"tool": ToolSpec("t", "d", {})}],
    )  # fmt: skip
    def test_any_key_ingredient_change_is_a_miss(self, change: dict[str, Any]) -> None:
        assert cache_key(req(**change)) != cache_key(req())

    def test_system_prompt_text_is_not_part_of_the_key(self) -> None:
        # The system prompt is derived from (prompt_version, schema_hash); row counts inside it
        # must not invalidate cached compilations.
        assert cache_key(req(system="A")) == cache_key(req(system="B"))

    def test_second_identical_call_skips_the_backend_and_costs_nothing(
        self, tmp_path: Path
    ) -> None:
        backend = ScriptedBackend(text_completion("answer", (2000, 300)))
        log = tmp_path / "c.jsonl"
        llm = LLMClient(backend, cache=FileCache(tmp_path / "cache"), cost_log=CostLog(log))
        first, second = llm.complete(req()), llm.complete(req())
        assert (first.cached, second.cached) == (False, True)
        assert second.text == "answer" and second.usd == 0.0 and second.usage == Usage()
        assert len(backend.requests) == 1
        miss, hit = entries(log)
        assert hit["cached"] is True and hit["usd"] == 0 and hit["input_tokens"] == 0
        assert hit["saved_usd"] == pytest.approx(miss["usd"])

    def test_cache_survives_a_new_client_instance(self, tmp_path: Path) -> None:
        LLMClient(ScriptedBackend(text_completion("v1")), cache=FileCache(tmp_path)).complete(req())
        backend = ScriptedBackend()  # would raise if called
        assert LLMClient(backend, cache=FileCache(tmp_path)).complete(req()).text == "v1"

    def test_tool_output_round_trips(self, tmp_path: Path) -> None:
        payload = {"ir": {"tables": ["leads"], "limit": None, "nested": [1, 2.5, "x"]}}
        raw = RawCompletion(None, payload, Usage(10, 5))
        llm = LLMClient(ScriptedBackend(raw), cache=FileCache(tmp_path))
        llm.complete(req())
        assert llm.complete(req()).tool_input == payload

    def test_uncacheable_results_are_not_stored(self, tmp_path: Path) -> None:
        backend = ScriptedBackend(text_completion("bad"), text_completion("good"))
        llm = LLMClient(backend, cache=FileCache(tmp_path))
        veto = lambda raw: raw.text == "good"  # noqa: E731
        assert llm.complete(req(), cacheable=veto).text == "bad"
        assert llm.complete(req(), cacheable=veto).text == "good"  # regenerated, not replayed
        assert llm.complete(req(), cacheable=veto).cached  # now cached

    def test_empty_responses_are_not_cached(self) -> None:
        backend = ScriptedBackend(RawCompletion(None, None, Usage(5, 0)), text_completion("ok"))
        llm = LLMClient(backend, cache=MemoryCache())
        llm.complete(req())
        assert llm.complete(req()).text == "ok"

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "not json",
            "{}",
            '{"text": 5, "tool_input": null, "input_tokens": 1, "output_tokens": 1}',
            "[1]",
        ],
    )
    def test_corrupt_cache_files_are_misses_and_get_repaired(
        self, tmp_path: Path, junk: str
    ) -> None:
        cache = FileCache(tmp_path)
        r = req()
        path = tmp_path / cache_key(r)[:2] / f"{cache_key(r)}.json"
        path.parent.mkdir(parents=True)
        path.write_text(junk)
        llm = LLMClient(ScriptedBackend(text_completion("fresh")), cache=cache)
        assert llm.complete(r).text == "fresh"
        assert llm.complete(r).cached

    def test_backend_failure_is_not_cached(self, tmp_path: Path) -> None:
        from llm import LLMError

        backend = ScriptedBackend(LLMError("boom"), text_completion("ok"))
        llm = LLMClient(backend, cache=FileCache(tmp_path))
        with pytest.raises(LLMError):
            llm.complete(req())
        assert llm.complete(req()).text == "ok"


class TestNoOpus:
    @pytest.mark.parametrize(
        "model",
        ["claude-opus-4-1", "claude-opus-4-6", "claude-opus-5", "claude-opus-5-5", "CLAUDE-OPUS-4",
         "anthropic.claude-opus-4", "my-opus-alias"],
    )  # fmt: skip
    def test_opus_is_refused_before_anything_else(self, model: str, tmp_path: Path) -> None:
        backend = ScriptedBackend(text_completion("x"))
        llm = LLMClient(backend, cache=MemoryCache(), cost_log=CostLog(tmp_path / "c.jsonl"))
        with pytest.raises(ModelNotAllowedError):
            llm.complete(req(model=model))
        assert backend.requests == [] and not (tmp_path / "c.jsonl").exists()

    def test_allowlisting_an_opus_model_by_mistake_still_cannot_call_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from llm.client import Pricing

        monkeypatch.setitem(client_mod.ALLOWED_MODELS, "claude-opus-4-6", Pricing(5.0, 25.0))  # type: ignore[index]
        backend = ScriptedBackend(text_completion("x"))
        with pytest.raises(ModelNotAllowedError):
            LLMClient(backend).complete(req(model="claude-opus-4-6"))
        assert backend.requests == []

    def test_a_cached_response_cannot_be_served_for_a_forbidden_model(self) -> None:
        cache = MemoryCache()
        r = req(model="claude-opus-4-6")
        cache.put(cache_key(r), text_completion("leaked"))
        with pytest.raises(ModelNotAllowedError):
            LLMClient(ScriptedBackend(), cache=cache).complete(r)


class TestRepoWideRules:
    """Static checks over the source tree: the rules hold for code that does not exist yet."""

    @staticmethod
    def python_files() -> list[Path]:
        skip = {".venv", "node_modules", ".git", "tests", "alembic"}
        return [p for p in REPO.rglob("*.py") if not skip & set(p.relative_to(REPO).parts)]

    def test_only_the_llm_wrapper_imports_the_anthropic_sdk(self) -> None:
        offenders = []
        for path in self.python_files():
            text = path.read_text()
            if re.search(r"^\s*(import anthropic|from anthropic)", text, re.M):
                offenders.append(path.relative_to(REPO).as_posix())
        assert offenders == ["llm/client.py"]

    def test_no_http_calls_to_the_anthropic_api_outside_the_wrapper(self) -> None:
        for path in self.python_files():
            if path.relative_to(REPO).as_posix() == "llm/client.py":
                continue
            assert "api.anthropic.com" not in path.read_text(), path

    def test_no_opus_model_ids_in_source(self) -> None:
        pattern = re.compile(r"""["']claude-[a-z0-9.-]*opus[a-z0-9.-]*["']""", re.I)
        offenders = [
            p.relative_to(REPO).as_posix()
            for p in self.python_files()
            if pattern.search(p.read_text())
        ]
        assert offenders == []

    def test_model_ids_used_by_the_app_are_exactly_the_allowlist(self) -> None:
        pattern = re.compile(r"""["'](claude-[a-z0-9.-]+)["']""")
        found = set()
        for path in self.python_files():
            found |= set(pattern.findall(path.read_text()))
        assert found == {SONNET, HAIKU}
