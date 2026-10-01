"""LLM extraction of structured fields from a raw note (claude-haiku-4-5, forced tool call)."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.models import NoteSource, Sentiment
from llm import LLMClient, LLMRequest, ToolSpec
from llm.client import HAIKU
from llm.prompts import Prompt, load_prompt
from llm.types import RawCompletion

PROMPT_PATH = Path(__file__).parent / "prompt.md"
MAX_OBJECTION_CHARS = 200


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_hint: float | None = Field(
        default=None, ge=0, le=1e9, description="Prospect budget in US dollars, or null"
    )
    timeline: str | None = Field(default=None, max_length=200)
    objections: list[str] = Field(default_factory=list, max_length=10)
    sentiment: Sentiment

    @field_validator("timeline")
    @classmethod
    def _blank_timeline_is_none(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None

    @field_validator("objections")
    @classmethod
    def _clean_objections(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if v and v.strip()]
        if any(len(v) > MAX_OBJECTION_CHARS for v in cleaned):
            raise ValueError(f"objections must be at most {MAX_OBJECTION_CHARS} characters")
        return cleaned


TOOL = ToolSpec(
    name="record_note_extraction",
    description="Record the structured signals extracted from the note.",
    input_schema=Extraction.model_json_schema(),
)


def _is_valid(raw: RawCompletion) -> bool:
    try:
        Extraction.model_validate(raw.tool_input or {})
    except ValidationError:
        return False
    return True


class ExtractionError(Exception):
    """The model's output could not be turned into a valid `Extraction`."""


@dataclass(frozen=True)
class ExtractionResult:
    extraction: Extraction
    model: str
    prompt_version: str
    usd: float


class NoteExtractor:
    def __init__(self, llm: LLMClient, prompt: Prompt | None = None, model: str = HAIKU) -> None:
        self._llm = llm
        self._prompt = prompt or load_prompt(PROMPT_PATH)
        self._model = model

    def extract(self, text: str, source: NoteSource) -> ExtractionResult:
        result = self._llm.complete(
            LLMRequest(
                task="ingest_extract",
                model=self._model,
                system=self._prompt.body,
                user=f'<note source="{source.value}">\n{text}\n</note>',
                max_tokens=512,
                prompt_version=self._prompt.version,
                tool=TOOL,
            ),
            cacheable=_is_valid,
        )
        try:
            extraction = Extraction.model_validate(result.tool_input or {})
        except ValidationError as exc:
            raise ExtractionError(f"invalid extraction: {exc.errors()[0]['msg']}") from exc
        return ExtractionResult(extraction, self._model, self._prompt.version, result.usd)
