"""A refused coding tool call tells the agent the real reason."""

import secrets
import sqlite3
from pathlib import Path

import pytest

from agent_comms.native_tool_call import CodingCall
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ClaimTransitionError
from agent_comms.native_tool_call import CoordinationBusy, SelectedToolDenied
from agent_comms.selected_tool_broker import verify_sent_full_input


def reply(tmp_path: Path, error: Exception) -> str:
    """What the agent reads as the tool result when the owner refuses a call."""
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700, exist_ok=True)
    return CodingToolSocket(directory, secrets.token_hex(32), None).failure_response(error)["error"]


def test_edit_of_a_missing_file_inside_the_project_says_it_does_not_exist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    edit = CodingCall.decode(
        "call", "edit", {"path": "absent.py", "edits": [{"oldText": "1", "newText": "2"}]}
    )
    with pytest.raises(ClaimTransitionError) as refused:
        edit.allowed_claim().normalized(project)
    assert reply(tmp_path, refused.value) == f"File does not exist: {project / 'absent.py'}"


def test_busy_coordination_database_is_reported_as_busy(tmp_path):
    (tmp_path / "wire").mkdir(mode=0o700)
    path = tmp_path / "wire" / "coordination.sqlite3"
    with Coordination(str(path)) as store:
        store.install_private_runtime()
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            with pytest.raises(CoordinationBusy) as busy:
                verify_sent_full_input(store, None, "owner", secrets.token_hex(16))
            writer.execute("ROLLBACK")
            message = reply(tmp_path, busy.value)
            assert "busy" in message and "did not run" in message
            assert "worktree" not in message and "UNKNOWN" not in message
            # With the database free the same call reaches the real check.
            with pytest.raises(SelectedToolDenied, match="no reserved FULL input") as denied:
                verify_sent_full_input(store, None, "owner", secrets.token_hex(16))
            assert not isinstance(denied.value, CoordinationBusy)
        finally:
            writer.close()
