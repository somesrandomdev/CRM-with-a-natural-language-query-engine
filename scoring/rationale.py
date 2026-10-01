"""LLM-written rationale for a score, with a guard that the model invented no numbers.

The score and breakdown are computed by `scoring.score_facts`; the model never sees a way to change
them and its text is discarded (replaced by a deterministic sentence) if it cites any figure that
is not in the data it was given.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from llm import LLMClient, LLMError, LLMRequest
from llm.client import HAIKU
from llm.prompts import Prompt, load_prompt
from scoring.scoring import ScoreResult

PROMPT_PATH = Path(__file__).parent / "prompt.md"
MAX_RATIONALE_CHARS = 500
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_OUT_OF_100 = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:/|out of)\s*100\b")


@dataclass(frozen=True)
class Rationale:
    text: str
    source: Literal["llm", "fallback"]
    usd: float = 0.0


def _payload(result: ScoreResult) -> dict[str, object]:
    return {
        "score": result.score,
        "out_of": 100,
        "breakdown": [
            {"rule": b.rule, "points": b.points, "max_points": b.max_points, "detail": b.detail}
            for b in result.breakdown
        ],
    }


def _allowed_numbers(result: ScoreResult) -> set[str]:
    allowed = {str(result.score)}
    for b in result.breakdown:
        allowed |= {str(b.points), str(b.max_points)}
        allowed |= {_normalise(m) for m in _NUMBER.findall(b.detail)}
    return allowed


def _normalise(number: str) -> str:
    return number.replace(",", "").rstrip(".")


def cites_only_known_numbers(text: str, result: ScoreResult) -> bool:
    """True if every figure in `text` appears in the score data.

    "N out of 100" / "N/100" is the one place 100 may appear, and N must be the actual score;
    a bare 100 is only allowed if it comes from a rule detail (e.g. a $100 budget).
    """
    for claimed in _OUT_OF_100.findall(text):
        if _normalise(claimed) != str(result.score):
            return False
    remaining = _OUT_OF_100.sub("", text)
    allowed = _allowed_numbers(result)
    return all(_normalise(n) in allowed for n in _NUMBER.findall(remaining))


def fallback_rationale(result: ScoreResult) -> str:
    best = max(result.breakdown, key=lambda b: (b.points / b.max_points, b.points))
    worst = min(result.breakdown, key=lambda b: (b.points / b.max_points, -b.points))
    text = (
        f"Scored {result.score}/100. Strongest factor: {best.rule} "
        f"({best.points}/{best.max_points}, {best.detail})."
    )
    if worst is not best:
        text += f" Weakest: {worst.rule} ({worst.points}/{worst.max_points}, {worst.detail})."
    return text


def explain_score(
    llm: LLMClient, result: ScoreResult, prompt: Prompt | None = None, model: str = HAIKU
) -> Rationale:
    prompt = prompt or load_prompt(PROMPT_PATH)
    try:
        completion = llm.complete(
            LLMRequest(
                task="score_rationale",
                model=model,
                system=prompt.body,
                user="<data>\n" + json.dumps(_payload(result), sort_keys=True) + "\n</data>",
                max_tokens=200,
                prompt_version=prompt.version,
            ),
            cacheable=lambda raw: _acceptable(raw.text, result),
        )
    except LLMError:
        return Rationale(fallback_rationale(result), "fallback")
    text = " ".join((completion.text or "").split())
    if not _acceptable(text, result):
        return Rationale(fallback_rationale(result), "fallback", completion.usd)
    return Rationale(text, "llm", completion.usd)


def _acceptable(text: str | None, result: ScoreResult) -> bool:
    cleaned = " ".join((text or "").split())
    return (
        bool(cleaned)
        and len(cleaned) <= MAX_RATIONALE_CHARS
        and cites_only_known_numbers(cleaned, result)
    )
