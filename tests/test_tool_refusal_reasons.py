"""A refused coding tool call tells the agent the real reason."""

import sqlite3

import pytest

from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.execution.channel_coding_tools import CodingToolSocket
from agent_comms.execution.selected.tool_broker import verify_sent_full_input
from agent_comms.nominal.field_codec import InputId
from agent_comms.transport.native_tool_call import AwaitingClaim, ExecutingCall, SelectedToolBusy
from agent_comms.transport.native_tools import NativeToolDeclaration
from agent_comms.transport.tool_denial import SelectedToolDenied
from agent_comms_hooks import ToolRefusedError


def reply(error: Exception) -> str:
    """What the agent reads as the tool result when the owner refuses a call."""
    return CodingToolSocket.failure_response(error)["error"]


def test_edit_of_a_missing_file_inside_the_project_says_it_does_not_exist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    edit = NativeToolDeclaration.decode(
        "call", "edit", {"path": "absent.py", "edits": [{"oldText": "1", "newText": "2"}]}
    )
    with pytest.raises(ToolRefusedError) as refused:
        edit.allowed_claim().normalized(project)
    assert reply(refused.value) == f"File does not exist: {project / 'absent.py'}"


def test_a_terminal_before_any_claim_records_the_refusal_instead_of_failing_the_turn(tmp_path):
    """A call the harness refused never reaches consume, so it owes no terminal.

    AwaitingClaim.consume moves the claim to UncertainClaim before awaiting the action,
    so a claim still awaiting at terminal proves the action never started. Raising there
    turned a refused call into a failed turn under a message naming the wrong cause.
    """
    def pending():
        return NativeToolDeclaration.decode(
            "call", "edit", {"path": "owner.py", "edits": [{"oldText": "1", "newText": "2"}]}
        )

    refused = pending()
    assert isinstance(refused.progress.claim_scope, AwaitingClaim)
    refused.progress.observation = ExecutingCall()
    refused.finish("edit", True, tmp_path / "coordination.sqlite3", InputId.mint(), 0)

    succeeded = pending()
    succeeded.progress.observation = ExecutingCall()
    with pytest.raises(SelectedToolDenied, match="succeeded without owner claim_scope"):
        succeeded.finish("edit", False, tmp_path / "coordination.sqlite3", InputId.mint(), 0)


def test_busy_coordination_database_is_reported_as_busy(tmp_path, private_comms):
    path = CoordinationStore.path_in(private_comms.root)
    with Coordination(str(path)) as store:
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            with pytest.raises(SelectedToolBusy) as busy:
                verify_sent_full_input(store, None, "owner", InputId.mint())
            writer.execute("ROLLBACK")
            assert "busy" in reply(busy.value)
            # With the database free the same call reaches the real check.
            with pytest.raises(SelectedToolDenied):
                verify_sent_full_input(store, None, "owner", InputId.mint())
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_owned_native_edit_of_a_missing_file_returns_the_refusal(
        tmp_path, native_runtime_package):
    """An acquired native owner returns admission's error through its actual socket."""
    import asyncio
    import json

    from agent_comms.execution.queued_input import InitialInput
    from conftest import answer_rules
    from native_backend_fixture import native_backend_fixture

    async with native_backend_fixture(tmp_path, package=native_runtime_package) as saved:
        await saved.author_history()
        async with saved.open_owner(native_options=(), auto_wake=False) as (agent, session_id):
            worker = agent.sessions.require(session_id)
            registry = agent._comms.registry
            owner = registry.require(worker)
            missing = registry.storage_rule.roots_of(owner).scratch / "absent.py"
            assert not missing.exists()
            saved.provider.tool_call = (
                "edit", {"path": str(missing), "edits": [{"oldText": "1", "newText": "2"}]},
            )
            answer_rules(agent._comms, worker)
            async with asyncio.timeout(40):
                await InitialInput.run(
                    agent.inputs, session_id, worker, "Exercise the missing-file edit",
                    display_text="Exercise the missing-file edit", input_id=InputId.mint(),
                )
            # Pi's own session writer records the real tool result; no policy,
            # turn, permission token or tool-execution event is fabricated here.
            messages = (json.loads(line) for line in saved.session.read_text().splitlines())
            results = [row["message"] for row in messages
                       if row.get("type") == "message"
                       and row["message"].get("role") == "toolResult"]
            assert len(results) == 1
            result = results[0]
            assert result["toolName"] == "edit"
            assert result["isError"] is True
            assert f"File does not exist: {missing}" in "\n".join(
                part["text"] for part in result["content"] if part["type"] == "text"
            )
            assert saved.provider.posts == 2, "The native turn must resume after its refusal"
            assert not missing.exists()
