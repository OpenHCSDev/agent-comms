"""Every native turn publishes the tool catalog before Pi launches, so one tool whose schema
cannot be built (a Core-internal command holding a whole Todo recurses through its history)
stops every teammate's full turn."""

from agent_comms.transport.tool_declaration import NativeRequestTools


def test_the_native_tool_catalog_builds_every_tool_schema():
    catalog = NativeRequestTools.catalog
    assert len(catalog) == len(NativeRequestTools.members_with(NativeRequestTools))


def test_tool_discovery_loads_the_requested_declarations_without_changing_the_bus(comms):
    import pytest

    from agent_comms.nominal.declared_family import UnknownFamilyMember
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.thread_identity import ToolName
    from agent_comms.transport.tool_caller import NoThreadCaller
    from agent_comms.transport.tools.served import (
        CommsDiscoverTool,
        NamedToolLookup,
        SearchToolLookup,
    )

    before = comms.registry.snapshot(), comms.bus.bus.revision()
    names = (ToolName("comms_order"), ToolName("comms_send"), ToolName("comms_order"))
    selected = CommsDiscoverTool(selection=NamedToolLookup(names)).apply(comms, NoThreadCaller())
    assert tuple(tool.name for tool in selected.tools) == names[:2]
    search = CommsDiscoverTool(selection=SearchToolLookup("message outcomes")).apply(comms, NoThreadCaller())
    assert ToolName("comms_message_outcomes") in tuple(tool.name for tool in search.tools)
    assert FieldCodec.encode(selected)["tools"][0]["name"] == "comms_order"
    with pytest.raises(UnknownFamilyMember):
        CommsDiscoverTool(selection=NamedToolLookup((ToolName("no_such_tool"),))).apply(comms, NoThreadCaller())
    assert (comms.registry.snapshot(), comms.bus.bus.revision()) == before
