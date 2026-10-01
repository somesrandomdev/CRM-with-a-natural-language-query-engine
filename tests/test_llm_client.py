import pytest

from llm import ALLOWED_MODELS, LLMClient, LLMRequest, ModelNotAllowedError, Usage
from llm.client import HAIKU, SONNET, compute_usd
from tests.fakes import ScriptedBackend, text_completion


def request(model: str) -> LLMRequest:
    return LLMRequest(
        task="t", model=model, system="s", user="u", max_tokens=10, prompt_version="1"
    )


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-1",
        "claude-opus-4-6",
        "claude-opus-5",
        "claude-opus-5-5",
        "opus",
        "claude-fable-5-1",
        "",
        "gpt-5",
    ],
)
def test_models_outside_the_allowlist_never_reach_the_backend(model: str) -> None:
    backend = ScriptedBackend(text_completion("x"))
    with pytest.raises(ModelNotAllowedError):
        LLMClient(backend).complete(request(model))
    assert backend.requests == []


def test_allowlist_contains_no_opus() -> None:
    assert set(ALLOWED_MODELS) == {SONNET, HAIKU}
    assert not any("opus" in m.lower() for m in ALLOWED_MODELS)


def test_cost_is_computed_from_usage() -> None:
    assert compute_usd(SONNET, Usage(1_000_000, 1_000_000)) == 18.0
    assert compute_usd(HAIKU, Usage(1_000_000, 1_000_000)) == 6.0
    assert compute_usd(SONNET, Usage(1000, 200)) == pytest.approx(0.006)
    assert compute_usd(SONNET, Usage(0, 0)) == 0.0


def test_result_carries_usd_and_model() -> None:
    client = LLMClient(ScriptedBackend(text_completion("hi", (1000, 100))))
    result = client.complete(request(SONNET))
    assert result.text == "hi"
    assert result.usd == pytest.approx(0.0045)
    assert result.model == SONNET
