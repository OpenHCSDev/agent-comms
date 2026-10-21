"""The real PreToolUse command refuses a merge that skips the merge queue."""

import json
import subprocess
import sys

import pytest


def pre_tool_use(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.queue_bypass"],
        input=json.dumps({"session_id": "s", "hook_event_name": "PreToolUse",
                          "tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("command, refused", [
    ("gh pr merge 2557 --admin --merge", "--admin"),
    ("cd x && gh pr merge 12 --merge --admin -R OpenHCSDev/toad", "--admin"),
    ("gh api -X PUT repos/OpenHCSDev/agent-comms/pulls/2557/merge -f merge_method=merge",
     "pulls/N/merge"),
    ("/usr/bin/gh api --method PUT /repos/OpenHCSDev/toad/pulls/1133/merge", "pulls/N/merge"),
])
def test_a_merge_around_the_queue_is_denied(command, refused):
    result = pre_tool_use(command)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert refused in decision["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "gh pr merge 2557 --auto --merge",
    "gh api repos/OpenHCSDev/agent-comms/pulls/2557",
    "gh api repos/OpenHCSDev/agent-comms/pulls/2557/files",
    'git commit -m "gh pr merge --admin is refused"',
    "echo gh pr merge --admin",
])
def test_queue_merges_and_reads_pass(command):
    result = pre_tool_use(command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
