"""Real native tool errors complete their call; they do not fail the input."""

from agent_comms.pi_payloads import ReportedFlag, ReportedText
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from agent_comms.native_tool_call import ArgumentRefusal, CodingCall
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.pi_payloads import ToolCallContent


@pytest.mark.parametrize("name, arguments, encoded", [
    ("bash", {"command": "pwd", "timeout": None}, {"command": "pwd"}),
    ("read", {"path": "x", "offset": None, "limit": None}, {"path": "x"}),
])
def test_pi_optional_nulls_are_omitted_by_the_existing_codec(name, arguments, encoded):
    call = CodingCall.decode("optional-null", name, arguments)
    assert call.allowed_claim() is None
    assert call.arguments == encoded


@pytest.mark.parametrize("request_first", [False, True])
async def test_refused_arguments_complete_without_an_owner_callback(tmp_path, request_first, tool_listener):
    class Owner:
        async def allow(self, round_number, call):
            raise AssertionError("invalid arguments must never acquire authority")

    socket = CodingToolSocket(tool_listener(tmp_path, "a" * 64), Owner(), pid=os.getpid())
    socket.turn = SimpleNamespace(launch=SimpleNamespace(root=tmp_path))
    args = {"command": False}
    raw = (json.dumps({"token": socket.listener.token, "call_id": "bad", "name": "bash",
                       "arguments": {"command": "false"}}) + "\n").encode()
    pending = asyncio.create_task(socket.handle_request(raw)) if request_first else None
    if request_first:
        await asyncio.sleep(0)
    socket.announce((ToolCallContent(id="bad", name="bash", arguments=args),))
    socket.tool_started(ToolExecutionStart(tool_call_id=ReportedText("bad"), tool_name=ReportedText("bash"), args=args))
    with pytest.raises(SelectedToolDenied) as denied:
        await (pending if pending is not None else socket.handle_request(raw))
    assert "received bool" in socket.failure_response(denied.value)["error"]
    assert isinstance(socket.calls.require(0, "bad").progress.claim_scope, ArgumentRefusal)
    socket.tool_finished(ToolExecutionEnd(
        tool_call_id=ReportedText("bad"), tool_name=ReportedText("bash"), is_error=ReportedFlag(True)
    ), "b" * 32)
    socket.assert_complete()
    assert not (tmp_path / "coordination.sqlite3").exists()


