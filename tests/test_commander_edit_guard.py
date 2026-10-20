"""Real hook entrypoints and /proc session classification; no classifier substitution."""

from __future__ import annotations
from agent_comms.repository_name import RepositoryName

import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.send_guard import SourceChangeCommand
from agent_comms.repo import Repo


@pytest.fixture
def guard_entry(commander, commander_script, tmp_path):
    comms, _ = commander
    context, repo = commander_script
    context = context.model_copy(
        update={
            "repos": (Repo(checkout=repo, github=RepositoryName.from_wire("OpenHCSDev/agent-comms")),),
        }
    )
    context.write(tmp_path)
    process = subprocess.Popen(
        ["sleep", "120"],
        env={**os.environ, "CLAUDE_CODE_SESSION_ID": "p294-real-session"},
    )
    comms.registry.declare(
        replace(
            comms.registry.require("lead"),
            process_identity=ProcessIdentity.capture(process.pid),
        )
    )

    def invoke(tool_input, *, edit=False, cwd=repo, session="p294-real-session"):
        payload = {
            "session_id": session,
            "hook_event_name": "PreToolUse",
            "cwd": str(cwd),
            "tool_input": tool_input,
        }
        # This is the actual console entry's main and argument parser, in its own process.
        command = [
            sys.executable,
            "-c",
            "from agent_comms.commander.cli import main; main()",
            "--config",
            str(context.root / "commander.json"),
            "send-guard",
        ]
        if edit:
            command.append("edit-guard")
        result = subprocess.run(
            command, input=json.dumps(payload), text=True, capture_output=True, timeout=20
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout else None

    try:
        yield context, repo, invoke
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_repo_edit_is_refused_by_real_hook_entry(guard_entry):
    context, repo, invoke = guard_entry
    for path in (repo / "src/agent_comms/todos.py", context.worktrees / "unit/src/x.py"):
        response = invoke({"file_path": str(path)}, edit=True)
        decision = response["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == SourceChangeCommand.reason
    assert invoke({"file_path": "src/agent_comms/todos.py"}, edit=True)["hookSpecificOutput"][
        "permissionDecision"
    ] == "deny"
    assert (
        invoke({"file_path": str(repo / "src/x.py")}, edit=True, session="a-different-real-session")
        is None
    )


def test_scratch_and_state_edits_remain_writable(guard_entry):
    context, _, invoke = guard_entry
    for directory in (context.scratch, context.state):
        assert invoke({"file_path": str(directory / "notes.md")}, edit=True) is None


def test_git_commit_and_push_use_the_shared_directory_decoder(guard_entry, tmp_path):
    context, repo, invoke = guard_entry
    for command, cwd in (
        ("git commit -am fix", repo),
        ("git push origin HEAD", repo),
        (f"git -C {shlex.quote(str(repo))} commit -am fix", tmp_path),
        (f"git -C{shlex.quote(str(repo))} push", tmp_path),
        (f"cd {shlex.quote(str(repo))} && git commit -am fix", tmp_path),
        (f"git --work-tree={shlex.quote(str(repo))} push", tmp_path),
    ):
        response = invoke({"command": command}, cwd=cwd)
        assert (
            response["hookSpecificOutput"]["permissionDecisionReason"] == SourceChangeCommand.reason
        )
    assert invoke({"command": "git commit -am scratch"}, cwd=context.scratch) is None
    assert invoke({"command": 'printf "%s" "git commit -am quoted"'}, cwd=repo) is None
    assert invoke({"command": "git --exec-path"}, cwd=repo) is None
    assert invoke({"command": "git -C"}, cwd=repo)["hookSpecificOutput"][
        "permissionDecision"
    ] == "deny"


def test_declared_batch_landing_path_remains_allowed(guard_entry):
    _, repo, invoke = guard_entry
    assert (
        invoke({"command": f"bash {shlex.quote(str(SourceChangeCommand.landing_tool))}"}, cwd=repo)
        is None
    )
    response = invoke({
        "command": f"bash {shlex.quote(str(SourceChangeCommand.landing_tool))} && git commit -am fix"
    })
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Naming the exemption as an ordinary git argument cannot exempt a push.
    response = invoke(
        {"command": f"git push origin {shlex.quote(str(SourceChangeCommand.landing_tool))}"}
    )
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
