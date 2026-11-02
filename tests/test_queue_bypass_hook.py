"""The real PreToolUse hook preserves each bypass's pin and review contract."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.hook_requests import HookReply, QueueBypassRequest
from agent_comms.cli_bridges.session_watch import WatchClaim, WatchRecord
from hook_session_fixture import resident_hook, resident_session
from agent_comms.todos import QualifiedHeadVerdict, ReviewedHeadVerdict, RejectedHeadVerdict
from agent_comms_hooks.queue_bypass import MergeTarget, MergeTargetUnreadable, QueueBypass
from agent_comms_hooks.shell_commands import HereDocument, ShellCommand
from test_head_review import ACCEPTED, QUALIFIED, REASON, invoke, opened

pytest_plugins = ("test_head_review",)

ADMIN_MERGE = "gh pr merge 2557 --admin --merge"


def resident_tool_use(command: str, transcript: Path, bridge: ClaudeBridge) -> HookReply:
    return resident_hook(bridge, QueueBypassRequest, transcript,
                         tool_name=QueueBypassRequest.tool, tool_input={"command": command})


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


@pytest.mark.parametrize("command", [
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms --match-head-commit {head} -A author@example.com",
    "gh pr merge -A author@example.com 7 --admin --repo=OpenHCSDev/agent-comms --match-head-commit={head}",
    "gh pr --repo OpenHCSDev/agent-comms merge 7 --admin --match-head-commit {head}",
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms --body api --subject repos/OpenHCSDev/agent-comms/pulls/7/merge --match-head-commit {head}",
    "gh api -X PUT repos/OpenHCSDev/agent-comms/pulls/7/merge -f sha={head}",
    "gh api https://api.github.com/repos/OpenHCSDev/agent-comms/pulls/7/merge?x=y --method PUT --field sha={head}",
])
def test_merge_target_uses_the_executables_options(command, github_cli):
    head = "a" * 40
    simple, = ShellCommand.split(command.format(head=head))
    bypass, = QueueBypass.matching(simple)
    assert bypass.target() == MergeTarget("OpenHCSDev/agent-comms", 7, head)


@pytest.mark.parametrize("command", [
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms",
    "gh pr --repo OpenHCSDev/agent-comms merge 7 --admin",
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms --repo OpenHCSDev/agent-comms --match-head-commit {head}",
    "gh pr --subject merge other --admin -R OpenHCSDev/agent-comms --match-head-commit {head}",
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms --subject --match-head-commit={head}",
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms -A --match-head-commit={head}",
    "gh pr merge 7 --admin -R OpenHCSDev/agent-comms --unknown-option --match-head-commit={head}",
    "gh api repos/OpenHCSDev/agent-comms/pulls/7/merge -X PUT",
    "gh api repos/OpenHCSDev/agent-comms/pulls/7/merge -f sha={head} --input body.json",
    "gh api repos/OpenHCSDev/agent-comms/pulls/7/merge -f sha={head} -f sha={head}",
    "gh api repos/OpenHCSDev/agent-comms/pulls/7/merge -f sha={head} --field sha={head}",
])
def test_merge_target_refuses_an_absent_ambiguous_or_shadowed_pin(command, github_cli):
    simple, = ShellCommand.split(command.format(head="a" * 40))
    with pytest.raises(MergeTargetUnreadable):
        bypass, = QueueBypass.matching(simple)
        bypass.target()


@pytest.mark.parametrize("command", [
    "gh pr merge 7 --subject --admin -R OpenHCSDev/agent-comms",
    "gh api repos/OpenHCSDev/agent-comms/pulls/7 --input repos/OpenHCSDev/agent-comms/pulls/7/merge",
])
def test_option_data_does_not_select_a_merge_family(command, github_cli):
    simple, = ShellCommand.split(command)
    assert QueueBypass.matching(simple) == ()


@pytest.mark.parametrize("actor", ("lead", "worker"))
def test_api_merge_requires_the_pinned_recorded_ok_for_each_colleague(
        head_bus, reviewed_source, tmp_path, actor):
    comms, github, request = head_bus
    transcript = tmp_path / "s.jsonl"

    def command(head):
        return (f"gh api -X PUT repos/{request.repo.to_wire()}/pulls/{request.pr}/merge"
                f" -f merge_method=merge -f sha={head}")

    with resident_session(comms, tmp_path / "state", actor, "s") as bridge:
        def refused(head):
            result = resident_tool_use(command(head), transcript, bridge)
            assert result.status == 0, result.stderr
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            assert decision["permissionDecision"] == "deny"
            assert f"#{request.pr}" in decision["permissionDecisionReason"]
            assert head in decision["permissionDecisionReason"]
            assert "ok verdict" in decision["permissionDecisionReason"]

        refused(request.head)
        invoke(comms, request, ReviewedHeadVerdict(ACCEPTED))
        result = resident_tool_use(command(request.head), transcript, bridge)
        assert result.status == 0, result.stderr
        assert result.stdout == ""
        moved = opened(github, reviewed_source, request.pr, "b" * 40)
        refused(moved.head)
        # Core authorizes the saved pin; GitHub's precondition refuses that old pin after a push.
        result = resident_tool_use(command(request.head), transcript, bridge)
        assert result.status == 0, result.stderr
        assert result.stdout == ""
        opened(github, reviewed_source, request.pr, request.head)
        invoke(comms, request, RejectedHeadVerdict(REASON))
        refused(request.head)
        invoke(comms, request, QualifiedHeadVerdict(QUALIFIED))
        refused(request.head)


@pytest.mark.parametrize("actor", ("lead", "worker"))
@pytest.mark.parametrize("configured", (True, False))
def test_pinned_admin_merge_keeps_repository_scope_without_rank_or_saved_ok(
        head_bus, tmp_path, actor, configured):
    comms, _, request = head_bus
    repository = request.repo.to_wire() if configured else "foreign/unconfigured"
    command = (f"gh pr merge {request.pr} --admin --merge --repo {repository}"
               f" --match-head-commit {request.head}")
    with resident_session(comms, tmp_path / "state", actor, "s") as bridge:
        # The resident's original actor survives replacement launcher metadata.
        replacement = "worker" if actor == "lead" else "lead"
        WatchClaim("s").enrol(WatchRecord(replacement, str(comms.root), (), tmp_path / "elsewhere"))
        result = resident_tool_use(command, tmp_path / "s.jsonl", bridge)
        assert result.status == 0, result.stderr
        if configured:
            assert result.stdout == ""
        else:
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize("pin", ("", "--match-head-commit not-a-commit"))
def test_admin_merge_requires_a_valid_head_pin(head_bus, tmp_path, pin):
    comms, _, request = head_bus
    command = f"gh pr merge {request.pr} --admin --merge --repo {request.repo.to_wire()} {pin}"
    with resident_session(comms, tmp_path / "state", "lead", "s") as bridge:
        result = resident_tool_use(command, tmp_path / "s.jsonl", bridge)
        assert result.status == 0, result.stderr
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_escaped_quote_inside_double_quotes_is_one_word():
    command = """git grep -n "'watch'\\|\\"watch\\"" -- tools | cut -c1-9"""
    assert [list(simple.words) for simple in ShellCommand.parsed(command)] == [
        ["git", "grep", "-n", """'watch'\\|"watch\"""", "--", "tools"], ["cut", "-c1-9"],
    ]


def test_here_document_body_after_escaped_quote_is_not_shell():
    command = 'echo "a\\"b"; cat <<\'EOF\'\nit\'s "not shell\nEOF\necho done\n'
    parsed = tuple(ShellCommand.parsed(command))
    assert [list(simple.words) for simple in parsed] == [
        ["echo", 'a"b'], ["cat"], ["echo", "done"],
    ]
    assert parsed[1].redirections == (HereDocument('EOF'),)
