"""Every native turn publishes the tool catalog before Pi launches, so one tool whose schema
cannot be built stops every teammate's full turn."""

from agent_comms.tool_declaration import NativeRequestTools
from agent_comms.todos import TodoOperation


def test_the_native_tool_catalog_builds_every_tool_schema():
    catalog = NativeRequestTools.catalog()
    assert len(catalog) == len(NativeRequestTools.members_with(NativeRequestTools))


def test_tool_operations_are_only_the_commands_teammates_call():
    """A command holding a whole Todo is Core's own, never a tool argument: its schema recurses
    through the Todo's history back into the commands."""
    names = {member.__name__ for member in TodoOperation.members_with(TodoOperation)}
    assert {"AssignTodo", "ReleaseTodo", "WithdrawTodo"} <= names
    assert not names & {"LinkTodo", "DropTodo", "FoldSourceTodo", "FoldTargetTodo"}
