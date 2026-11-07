"""The generated deal prompt is install output, never a committed file.

`DealPrompt.prepare` publishes each deck under its own content name beside
`stack/SYSTEM_PROMPT.md`, and every checkout generates its own. Committing one pins a deck the
next install supersedes, and it breaks the next install that runs in a checkout holding its own
copy: `git merge` refuses to overwrite that untracked file, the installer rolls back, and the
bus stays on the old runtime.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATED = "stack/SYSTEM_PROMPT-0123456789abcdef.md"


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True)


@pytest.mark.parametrize("name", [
    GENERATED,
    f"{GENERATED}.reads.json",
])
def test_a_generated_deal_prompt_is_ignored_and_never_tracked(name: str) -> None:
    ignored = git("check-ignore", "--quiet", name)
    assert ignored.returncode == 0, f"{name} is not ignored; a checkout's own copy blocks installs"

    tracked = git("ls-files", "--", name)
    assert tracked.stdout == "", f"{name} is tracked; a later install cannot merge past it"


def test_no_generated_deal_prompt_is_tracked_at_all() -> None:
    tracked = git("ls-files", "--", "stack/SYSTEM_PROMPT-*.md", "stack/SYSTEM_PROMPT-*.md.reads.json")
    assert tracked.stdout == "", f"generated deal prompts are tracked: {tracked.stdout}"
