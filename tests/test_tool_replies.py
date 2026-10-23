"""Every declared tool's reply is the encoding of its declared result record."""
from typing import get_type_hints

from agent_comms.comms import Comms
from agent_comms.tool_result import ToolResult
from agent_comms.tool_request import ToolRequest


def result_type(tool: type[ToolRequest]) -> type:
    return get_type_hints(tool.apply, localns={"Comms": Comms})["return"]


def stores(comms: Comms) -> dict:
    """registry.json, the todo store and the goal store, byte for byte."""
    paths = (comms.root / "registry.json", comms.goals.todos.path,
             *(comms.root / "goal-private").rglob("*"))
    return {path: path.read_bytes() for path in paths if path.is_file()}


def test_every_tool_declares_a_tool_result():
    for tool in ToolRequest.members_with(ToolRequest):
        from typing import get_args
        result = result_type(tool)
        assert all(issubclass(member, ToolResult) for member in (get_args(result) or (result,))), tool.declared_name
