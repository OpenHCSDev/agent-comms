"""A hook runs on every tool call, so its import path never builds the Comms facade or the CLI.

On 2026-10-29 one import edge (read_gate -> forks -> comms.client_wire) put the whole facade
and the Todo store into every PreToolUse: 4.5 s of CPU per tool call per agent under load.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

FACADE = ("agent_comms.comms", "agent_comms.todos", "agent_comms.cli_commands")


@pytest.mark.parametrize("hook", ["agent_comms_hooks.read_gate", "agent_comms_hooks.seats"])
def test_per_call_hook_never_imports_the_facade(hook: str) -> None:
    loaded = subprocess.run(
        [sys.executable, "-c",
         f"import sys, {hook}; print(' '.join(m for m in {FACADE!r} if m in sys.modules))"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert loaded == []


@pytest.mark.parametrize("hook", ["agent_comms_hooks.bridged"])
def test_tool_call_hook_client_imports_no_core(hook: str) -> None:
    """The read gate and the reminder run on every tool call; the session bridge decides them,
    so their client loads no agent_comms module at all."""
    loaded = subprocess.run(
        [sys.executable, "-c",
         f"import sys, {hook}; print(' '.join(m for m in sys.modules if m.startswith('agent_comms.')))"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert loaded == []
