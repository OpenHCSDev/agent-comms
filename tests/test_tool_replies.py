"""Every declared tool's reply is the encoding of its declared result record."""
from typing import get_args

from agent_comms.transport.tool_request import ServedRequest
from agent_comms.transport.tool_result import ToolResult


def test_every_tool_declares_a_tool_result():
    for tool in ServedRequest.members_with(ServedRequest):
        result = tool.result_type()
        assert all(issubclass(member, ToolResult) for member in (get_args(result) or (result,))), tool.declared_name
