"""Fast stdlib CLI entry point parity and cold-start checks."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agent_comms.cli import discover_cli_verbs
from agent_comms.cli_verbs.cli_verb import CliCommand


def test_cli_discovers_exact_cli_command_catalog() -> None:
    expected = {member.declared_name: member.help for member in CliCommand.members_with(CliCommand)}
    assert discover_cli_verbs() == expected


def test_cli_help_answers_without_importing_runtime() -> None:
    checkout = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    result = subprocess.run(
        (sys.executable, "-m", "agent_comms.cli", "--help"),
        cwd=checkout,
        env={**os.environ, "PYTHONPATH": str(checkout / "src")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage: agent-comms" in result.stdout
    assert "channels" in result.stdout and "history" in result.stdout
    assert elapsed < 1.0, f"agent-comms --help took {elapsed:.3f}s"
