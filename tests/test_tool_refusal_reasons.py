"""A refused coding tool call tells the agent the real reason."""

import sqlite3
from pathlib import Path

import pytest

from agent_comms.native_tool_call import CodingCall
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.coordinator import Coordination
from agent_comms.coordination_database import CoordinationStore
from agent_comms.envelope_claim_transitions import ClaimTransitionError
from agent_comms.field_codec import InputId
from agent_comms.native_tool_call import CoordinationBusy
from agent_comms.tool_denial import SelectedToolDenied
from agent_comms.selected_tool_broker import verify_sent_full_input


def reply(tmp_path: Path, error: Exception) -> str:
    """What the agent reads as the tool result when the owner refuses a call."""
    return CodingToolSocket.failure_response(error)["error"]


def test_edit_of_a_missing_file_inside_the_project_says_it_does_not_exist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    edit = CodingCall.decode(
        "call", "edit", {"path": "absent.py", "edits": [{"oldText": "1", "newText": "2"}]}
    )
    with pytest.raises(ClaimTransitionError) as refused:
        edit.allowed_claim().normalized(project)
    assert reply(tmp_path, refused.value) == f"File does not exist: {project / 'absent.py'}"


def test_busy_coordination_database_is_reported_as_busy(tmp_path, private_comms):
    path = CoordinationStore.path_in(private_comms.root)
    with Coordination(str(path)) as store:
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            with pytest.raises(CoordinationBusy) as busy:
                verify_sent_full_input(store, None, "owner", InputId.mint())
            writer.execute("ROLLBACK")
            message = reply(tmp_path, busy.value)
            assert "busy" in message and "did not run" in message
            assert "worktree" not in message and "UNKNOWN" not in message
            # With the database free the same call reaches the real check.
            with pytest.raises(SelectedToolDenied) as denied:
                verify_sent_full_input(store, None, "owner", InputId.mint())
            assert not isinstance(denied.value, CoordinationBusy)
        finally:
            writer.close()
