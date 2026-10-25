"""The real PreToolUse command refuses a Bash wait loop that cannot end on its own."""

import json
import subprocess
import sys

import pytest


def pre_tool_use(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.bounded_waits"],
        input=json.dumps({"session_id": "s", "hook_event_name": "PreToolUse",
                          "transcript_path": "s.jsonl",
                          "tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("command, refused", [
    ("timeout 600 bash -c 'until ! pgrep -f verify; do sleep 5; done'", "pgrep -f"),
    ("until [ -s out.txt ]; do sleep 5; done", "timeout N"),
    ("timeout 60 bash -c 'until [ -s a ]; do sleep 5; done'; until [ -s b ]; do sleep 5; done",
     "timeout N"),
    ("ls; while true; do sleep 5; done", "timeout N"),
    ("bash -c 'until [ -s f ]; do sleep 5; done'", "timeout N"),
    ("timeout 7200 bash -c 'until [ -s f ]; do sleep 5; done'", "timeout N"),
    ("timeout soon bash -c 'until [ -s f ]; do sleep 5; done'", "timeout N"),
    ("timeout 60 bash -c 'while sleep 1 && pgrep -af verify; do :; done'", "pgrep -f"),
    ("timeout 60 bash -c 'until ! /usr/bin/pgrep -f verify; do sleep 5; done'", "pgrep -f"),
    ("VAR=x bash -c 'until [ -s f ]; do sleep 5; done'", "timeout N bash -c"),
    ("/bin/bash -c 'until [ -s f ]; do sleep 5; done'", "timeout N bash -c"),
    ("timeout 60 until [ -s f ]; do sleep 5; done", "timeout N bash -c"),
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
    "cat out.txt; timeout 60 bash -c 'until [ -s f ]; do sleep 5; done'",
    "git status",
    'judge back X --roast "slop because it restates while the owner already answers it"',
    'echo "until later"',
    "timeout -k 5 1h bash -c 'until [ -s f ]; do sleep 5; done'",
    """timeout 60 bash -c 'until grep -q "pgrep -f" log; do sleep 5; done'""",
    "timeout 60 bash -c 'until [ -s f ]; do sleep 5; done'; pgrep -f verify",
    "cat > notes.txt <<'EOF'\nwhile true; do sleep 5; done\nEOF",
    "X=1 timeout 60 /bin/bash -c 'until [ -s f ]; do sleep 5; done'",
])
def test_a_bounded_wait_or_plain_command_passes(command):
    result = pre_tool_use(command)
    assert (result.returncode, result.stdout) == (0, ""), result.stderr
