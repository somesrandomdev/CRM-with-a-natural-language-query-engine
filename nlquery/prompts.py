"""Locations of the query compiler's versioned prompts."""

from pathlib import Path

from llm.prompts import Prompt, load_prompt

PROMPT_DIR = Path(__file__).parent
COMPILER_PROMPT_PATH = PROMPT_DIR / "prompt.md"
EXPLAINER_PROMPT_PATH = PROMPT_DIR / "explain_prompt.md"

__all__ = ["COMPILER_PROMPT_PATH", "EXPLAINER_PROMPT_PATH", "Prompt", "load_prompt"]
