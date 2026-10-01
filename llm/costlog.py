"""Append-only JSONL log of every LLM call, plus an optional daily spend cap.

One line per call: {ts, task, model, input_tokens, output_tokens, usd, cached, saved_usd,
prompt_version}. Prompts and responses are never written, only metadata.
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from llm.types import LLMError

log = logging.getLogger(__name__)


class BudgetExceededError(LLMError):
    """The configured daily LLM budget is used up."""


@dataclass(frozen=True)
class CostEntry:
    task: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float
    cached: bool
    prompt_version: str
    saved_usd: float = 0.0


class CostLog:
    def __init__(self, path: Path, daily_budget_usd: float | None = None) -> None:
        self._path = path
        self._budget = daily_budget_usd
        self._lock = threading.Lock()
        self._day: date = datetime.now(UTC).date()
        self._spent_today = self._load_today() if daily_budget_usd is not None else 0.0

    def _load_today(self) -> float:
        today = self._day.isoformat()
        total = 0.0
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(entry.get("ts", "")).startswith(today):
                    total += float(entry.get("usd", 0.0))
        except FileNotFoundError:
            pass
        return total

    def check_budget(self) -> None:
        """Raise before making a call if today's spend has already reached the cap."""
        if self._budget is None:
            return
        with self._lock:
            self._roll_day()
            if self._spent_today >= self._budget:
                raise BudgetExceededError(
                    f"daily LLM budget ${self._budget:.2f} reached (spent ${self._spent_today:.4f})"
                )

    def _roll_day(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day, self._spent_today = today, 0.0

    def record(self, entry: CostEntry) -> None:
        line = json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "task": entry.task,
                "model": entry.model,
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
                "usd": entry.usd,
                "cached": entry.cached,
                "saved_usd": entry.saved_usd,
                "prompt_version": entry.prompt_version,
            },
            separators=(",", ":"),
        )
        with self._lock:
            self._roll_day()
            self._spent_today += entry.usd
            try:
                # A single O_APPEND write of one short line is atomic across processes.
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                # Money was already spent: never fail the user's request over bookkeeping, but
                # make the gap loud.
                log.exception("FAILED TO WRITE COST LOG ENTRY: %s", line)
