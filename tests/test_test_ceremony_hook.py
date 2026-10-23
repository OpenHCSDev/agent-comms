"""The real PreToolUse command refuses a local run of the whole test suite."""

import json
import subprocess
import sys

import pytest


def pre_tool_use(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.test_ceremony"],
        input=json.dumps({"session_id": "s", "hook_event_name": "PreToolUse",
                          "tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("command", [
    "python -m pytest -q -n 8 tests",
    "pytest -q -n0 -p no:cacheprovider",
    "cd x && PYTHONPATH=src timeout 900 .venv/bin/python -m pytest -q --basetemp=/b tests/",
    "bash -c 'cd /w && timeout 1500 python -m pytest -q -n 8 --durations=80 tests > l 2>&1'",
])
def test_a_whole_suite_run_is_denied(command):
    result = pre_tool_use(command)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("Test ceremony refused")


@pytest.mark.parametrize("command", [
    "python -m pytest -q -n0 tests/test_channels.py",
    "bash -c 'timeout 300 python -m pytest -q -n4 tests/test_a.py tests/test_b.py::test_c'",
    "python -m pytest -q -k slow tests/guards/test_debt_ratchet.py",
    'git commit -m "pytest tests is refused"',
    "python -I tools/debt_ratchet.py --root src",
])
def test_targeted_runs_and_other_commands_pass(command):
    result = pre_tool_use(command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_merge_hidden_in_bash_c_is_seen():
    """The shell's `-c` string is decoded like any command line, for every hook."""
    from agent_comms_hooks.shell import ShellCommand

    programs = [command.program
                for command in ShellCommand.split("bash -c 'cd x && gh pr merge 1 --admin'")]
    assert programs == ["bash", "cd", "gh"]


def test_a_command_started_by_a_runner_is_seen():
    """`timeout`, `env` and their kin run their arguments; the command they start is read."""
    from agent_comms_hooks.shell import ShellCommand

    programs = [command.program for command in ShellCommand.split(
        "timeout 60 env A=1 gh pr merge 1 --admin")]
    assert programs == ["timeout", "env", "gh"]
