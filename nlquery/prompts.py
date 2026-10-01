"""Loads versioned prompt files (`---\\nversion: X\\n---\\n<body>`)."""

import re
from dataclasses import dataclass
from pathlib import Path

_FRONT_MATTER = re.compile(r"\A---\nversion:\s*(?P<version>\S+)\n---\n", re.ASCII)


@dataclass(frozen=True)
class Prompt:
    version: str
    body: str

    def render(self, **values: str) -> str:
        text = self.body
        for key, value in values.items():
            placeholder = "{{" + key + "}}"
            if placeholder not in text:
                raise KeyError(f"prompt has no placeholder {placeholder}")
            text = text.replace(placeholder, value)
        return text


def load_prompt(path: Path) -> Prompt:
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise ValueError(f"{path} is missing its `---\\nversion: X\\n---` header")
    return Prompt(version=match["version"], body=raw[match.end() :])


PROMPT_DIR = Path(__file__).parent
COMPILER_PROMPT_PATH = PROMPT_DIR / "prompt.md"
EXPLAINER_PROMPT_PATH = PROMPT_DIR / "explain_prompt.md"
