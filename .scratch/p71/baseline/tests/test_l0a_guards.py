"""L0A removes format recovery paths from its complete owned source surface."""

import re
from pathlib import Path

import pytest


@pytest.mark.refactor_guard
def test_owned_loaders_have_no_retired_mechanisms():
    root = Path(__file__).parents[1] / "src" / "agent_comms"
    forbidden = re.compile(r"legacy|compat|deprecat|backward|fallback|shim", re.IGNORECASE)
    findings = [
        f"{name}:{number}: {line.strip()}"
        for name in (
            "goals.py",
            "registry_document.py",
            "registry_store.py",
            "registration.py",
            "goal_history.py",
            "thread_management.py",
            "cli_commands.py",
        )
        for number, line in enumerate((root / name).read_text().splitlines(), 1)
        if forbidden.search(line)
    ]
    assert not findings, "\n".join(findings)
