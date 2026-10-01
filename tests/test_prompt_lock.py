"""Prompt edits must come with a version bump.

Cache keys include the prompt version, so editing a prompt without bumping it would keep serving
answers compiled by the old prompt. Changing a prompt body fails this test until the new version
and hash are recorded here, in the same commit as the prompt change.
"""

import hashlib

import pytest

from nlquery.prompts import COMPILER_PROMPT_PATH, EXPLAINER_PROMPT_PATH, load_prompt

LOCKED = {
    "compiler": (COMPILER_PROMPT_PATH, "1.0.0", "239c2f789077daa5"),
    "explainer": (EXPLAINER_PROMPT_PATH, "1.0.0", "4ffc55a351e4edc0"),
}


@pytest.mark.parametrize("name", LOCKED)
def test_prompt_matches_its_locked_version(name: str) -> None:
    path, version, digest = LOCKED[name]
    prompt = load_prompt(path)
    actual = hashlib.sha256(prompt.body.encode()).hexdigest()[:16]
    assert (prompt.version, actual) == (version, digest), (
        f"{path.name} changed: bump `version:` in the file, then lock "
        f"({prompt.version!r}, {actual!r}) here"
    )
