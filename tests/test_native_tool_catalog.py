"""Every native turn publishes the tool catalog before Pi launches, so one tool whose schema
cannot be built (a Core-internal command holding a whole Todo recurses through its history)
stops every teammate's full turn."""

from agent_comms.tool_declaration import NativeRequestTools


def test_the_native_tool_catalog_builds_every_tool_schema():
    catalog = NativeRequestTools.catalog()
    assert len(catalog) == len(NativeRequestTools.members_with(NativeRequestTools))
