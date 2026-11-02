"""A refused coding tool call tells the agent the real reason."""

import sqlite3
import pytest

from agent_comms_hooks import ToolRefusedError
from agent_comms.envelope_claim_transitions import FileClaimDenied
from agent_comms.maintenance_barrier import MaintenanceHeldError

from agent_comms.native_tool_call import CodingCall
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.coordinator import Coordination
from agent_comms.coordination_database import CoordinationStore
from agent_comms.nominal.field_codec import InputId
from agent_comms.native_tool_call import SelectedToolBusy
from agent_comms.tool_denial import SelectedToolDenied
from agent_comms.selected_tool_broker import verify_sent_full_input


def reply(error: Exception) -> str:
    """What the agent reads as the tool result when the owner refuses a call."""
    return CodingToolSocket.failure_response(error)["error"]


def test_edit_of_a_missing_file_inside_the_project_says_it_does_not_exist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    edit = CodingCall.decode(
        "call", "edit", {"path": "absent.py", "edits": [{"oldText": "1", "newText": "2"}]}
    )
    with pytest.raises(ToolRefusedError) as refused:
        edit.allowed_claim().normalized(project)
    assert reply(refused.value) == f"File does not exist: {project / 'absent.py'}"


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
@pytest.mark.parametrize("denial", (
    ToolRefusedError, SelectedToolDenied, FileClaimDenied, MaintenanceHeldError,
))
async def test_declared_refusal_returns_a_socket_answer(
        tmp_path, tool_listener, undealt_gate, denial):
    """The transport must answer a declared refusal, rather than lose the connection."""
    import asyncio
    import json
    import os

    from agent_comms.pi_payloads import AssistantMessage, ReportedParts, ReportedText, ToolCallContent
    from agent_comms.pi_events import ToolExecutionStart

    class RefusingOwner:
        async def allow(self, turn, round_number, call):
            raise denial("Admission refused before execution")

    tmp_path.chmod(0o700)
    socket = CodingToolSocket(
        tool_listener(tmp_path, "b" * 64), RefusingOwner(), undealt_gate, pid=os.getpid())
    socket.turn = object()
    args = {"path": "absent.py", "edits": [{"oldText": "1", "newText": "2"}]}
    socket.announce(AssistantMessage(content=ReportedParts((
        ToolCallContent(id="denied", name="edit", arguments=args),
    ))))
    socket.tool_started(ToolExecutionStart(
        tool_call_id=ReportedText("denied"), tool_name=ReportedText("edit"), args=args,
    ))
    await socket.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket.listener.address))
        try:
            request = {"token": socket.listener.token, "call_id": "denied",
                       "name": "edit", "arguments": args}
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            async with asyncio.timeout(5):
                response = json.loads(await reader.readline())
            assert response == {"ok": False, "error": "Admission refused before execution"}
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await socket.close()
