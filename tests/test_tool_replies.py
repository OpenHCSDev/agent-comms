"""Every declared tool's reply is the encoding of its declared result record."""
from types import get_original_bases
from typing import TypeVar, get_args, get_origin, get_type_hints

import pytest

from agent_comms.bus_read import BusRead
from agent_comms.comms import Comms
from agent_comms.tool_result import ToolResult
from agent_comms.tool_request import ToolRequest


def bus_read_result(tool: type[BusRead]) -> type:
    """The result a bus read declares once: the first concrete specialisation of a BusRead
    capability along its MRO. A family header still carrying its type parameter declares
    nothing; two different concrete answers contradict each other."""
    declared = tuple(dict.fromkeys(
        argument
        for declaration in tool.__mro__
        for base in get_original_bases(declaration)
        if isinstance(get_origin(base), type) and issubclass(get_origin(base), BusRead)
        for argument in get_args(base)
        if not isinstance(argument, TypeVar)
    ))
    if len(declared) != 1:
        raise TypeError(f"{tool.__qualname__} declares results {declared!r}, not exactly one")
    return declared[0]


def apply_result(tool: type[ToolRequest]) -> type:
    return get_type_hints(tool.apply, localns={
        "Comms": Comms,
        **{param.__name__: param for param in ToolRequest.__type_params__},
    })["return"]


def result_type(tool: type[ToolRequest]) -> type:
    if issubclass(tool, BusRead):
        return bus_read_result(tool)
    return apply_result(tool)


def test_bus_read_result_follows_an_inherited_specialisation_and_refuses_an_unspecialised_family():
    from agent_comms.bus_read import OwnerLiveRead, UiRead
    from agent_comms.notification_evidence import NotificationWindow
    from agent_comms.notification_reads import RecordedNotificationRead

    # RecordedNotificationRead restates no header: it inherits NotificationRead's UiRead[...].
    assert bus_read_result(RecordedNotificationRead) is NotificationWindow
    for family in (BusRead, UiRead, OwnerLiveRead):
        with pytest.raises(TypeError, match="not exactly one"):
            bus_read_result(family)


def test_an_ordinary_tool_keeps_its_apply_annotation():
    ordinary = next(tool for tool in ToolRequest.members_with(ToolRequest) if not issubclass(tool, BusRead))
    assert result_type(ordinary) is apply_result(ordinary)


def stores(comms: Comms) -> dict:
    """registry.json, the todo store and the goal store, byte for byte."""
    paths = (comms.root / "registry.json", comms.goals.todos.path,
             *(comms.root / "goal-private").rglob("*"))
    return {path: path.read_bytes() for path in paths if path.is_file()}


def test_every_tool_declares_a_tool_result():
    for tool in ToolRequest.members_with(ToolRequest):
        result = result_type(tool)
        assert all(issubclass(member, ToolResult) for member in (get_args(result) or (result,))), tool.declared_name
