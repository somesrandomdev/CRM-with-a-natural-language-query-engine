"""Response cache for LLM calls.

Key = sha256(schema_hash, prompt_version, input), where `input` is the canonical JSON of the
request's task, model and user message. Everything else the model sees is a function of those
(the system prompt is rendered from the versioned prompt file and, for the compiler, the schema
that `schema_hash` identifies), so a prompt or schema change misses the cache automatically.
`tests/test_prompt_lock.py` makes "edited a prompt without bumping its version" a failing test.
"""

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from llm.types import LLMRequest, RawCompletion, Usage

log = logging.getLogger(__name__)

_SEP = "\x1f"


def cache_key(request: LLMRequest) -> str:
    request_input = json.dumps(
        {
            "task": request.task,
            "model": request.model,
            "user": request.user,
            "tool": request.tool.name if request.tool else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    material = _SEP.join([request.schema_hash, request.prompt_version, request_input])
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class CachedCompletion:
    completion: RawCompletion
    original_usage: Usage  # what the call cost when it was made (for "saved" accounting)


class ResponseCache(Protocol):
    def get(self, key: str) -> CachedCompletion | None: ...
    def put(self, key: str, completion: RawCompletion) -> None: ...


class NullCache:
    def get(self, key: str) -> CachedCompletion | None:
        return None

    def put(self, key: str, completion: RawCompletion) -> None:
        return None


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, RawCompletion] = {}

    def get(self, key: str) -> CachedCompletion | None:
        hit = self._data.get(key)
        return None if hit is None else CachedCompletion(hit, hit.usage)

    def put(self, key: str, completion: RawCompletion) -> None:
        self._data[key] = completion


class FileCache:
    """One JSON file per key, written atomically so concurrent processes never see a torn file."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> CachedCompletion | None:
        path = self._path(key)
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            usage = Usage(int(raw["input_tokens"]), int(raw["output_tokens"]))
            text, tool_input = raw["text"], raw["tool_input"]
            if text is not None and not isinstance(text, str):
                raise TypeError("text")
            if tool_input is not None and not isinstance(tool_input, dict):
                raise TypeError("tool_input")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable cache entry %s", path.name)
            return None  # a corrupt entry is just a miss; it is overwritten on the next put
        return CachedCompletion(RawCompletion(text, tool_input, usage), usage)

    def put(self, key: str, completion: RawCompletion) -> None:
        path = self._path(key)
        payload = {
            "text": completion.text,
            "tool_input": completion.tool_input,
            "input_tokens": completion.usage.input_tokens,
            "output_tokens": completion.usage.output_tokens,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except OSError:
            log.exception("could not write LLM cache entry")  # caching is best-effort
