"""The real PreToolUse hook requires Core's exact-head verdict for a commander bypass."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.todos import ReviewedHeadVerdict, RejectedHeadVerdict
from agent_comms_hooks.watch import WatchClaim, WatchRecord
from test_head_review import ACCEPTED, REASON, invoke, opened

pytest_plugins = ("test_head_review",)

ADMIN_MERGE = "gh pr merge 2557 --admin --merge"


def pre_tool_use(command: str, transcript: Path = Path("s.jsonl")) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.queue_bypass"],
        input=json.dumps({"session_id": "s", "hook_event_name": "PreToolUse",
                          "transcript_path": str(transcript),
                          "tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("command, refused", [
    (ADMIN_MERGE, "--admin"),
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


def test_commander_admin_and_api_merges_require_the_current_recorded_ok(
        head_bus, reviewed_source, tmp_path):
    comms, github, request = head_bus
    transcript = tmp_path / "s.jsonl"
    transcript.with_suffix(".reread").mkdir()
    WatchClaim("s").enrol(WatchRecord("lead", str(comms.root), (), tmp_path / "state"))
    commands = (
        f"gh pr merge {request.pr} --admin --merge --repo {request.repo.to_wire()}",
        f"gh api -X PUT repos/{request.repo.to_wire()}/pulls/{request.pr}/merge -f merge_method=merge",
    )

    def refused(head):
        for command in commands:
            result = pre_tool_use(command, transcript)
            assert result.returncode == 0, result.stderr
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            assert decision["permissionDecision"] == "deny"
            assert f"#{request.pr}" in decision["permissionDecisionReason"]
            assert head in decision["permissionDecisionReason"]
            assert "ok verdict" in decision["permissionDecisionReason"]

    refused(request.head)
    invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
    for command in commands:
        result = pre_tool_use(command, transcript)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    moved = opened(github, reviewed_source, request.pr, "b" * 40)
    refused(moved.head)
    opened(github, reviewed_source, request.pr, request.head)
    invoke(comms, request, RejectedHeadVerdict(REASON))
    refused(request.head)
