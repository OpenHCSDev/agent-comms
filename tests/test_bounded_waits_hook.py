"""The real PreToolUse command refuses a Bash wait loop that cannot end on its own."""

import json
import subprocess
import sys

import pytest


def pre_tool_use(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.bounded_waits"],
        input=json.dumps({"session_id": "s", "hook_event_name": "PreToolUse",
                          "tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("command, refused", [
    ("timeout 600 bash -c 'until ! pgrep -f verify; do sleep 5; done'", "pgrep -f"),
    ("until [ -s out.txt ]; do sleep 5; done", "timeout N"),
])
def test_an_unbounded_wait_is_denied(command, refused):
    result = pre_tool_use(command)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert refused in decision["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    'timeout 600 bash -c "until [ -s out.txt ]; do sleep 5; done"',
    "timeout 300 bash -c 'while kill -0 4242; do sleep 5; done'",
    "git status",
])
def test_a_bounded_wait_or_plain_command_passes(command):
    result = pre_tool_use(command)
    assert (result.returncode, result.stdout) == (0, ""), result.stderr
