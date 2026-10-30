"""A hook runs on every tool call, so its import path never builds the Comms facade or the CLI.

On 2026-10-29 one import edge (read_gate -> forks -> comms.client_wire) put the whole facade
and the Todo store into every PreToolUse: 4.5 s of CPU per tool call per agent under load.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import tomllib

import pytest

FACADE = ("agent_comms.comms", "agent_comms.todos", "agent_comms.cli_commands")


@pytest.mark.parametrize("hook", sorted({
    target.partition(":")[0]
    for target in tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["scripts"].values()
    if target.startswith("agent_comms_hooks.")
}))
def test_per_call_hook_never_imports_the_facade(hook: str) -> None:
    loaded = subprocess.run(
        [sys.executable, "-c",
         f"import sys, {hook}; print(' '.join(m for m in {FACADE!r} if m in sys.modules))"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert loaded == []
