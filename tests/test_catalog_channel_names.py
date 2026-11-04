"""A catalog's channel names are the channels it builds, read without activity clocks."""

from agent_comms.activity_clocks import ActivityClocks
from agent_comms.comms import Comms


def test_channel_names_are_exactly_the_channels_views_builds(comms: Comms) -> None:
    catalog = comms.channels.catalog.read()
    names = catalog.channel_names()
    assert names == tuple(catalog.views(ActivityClocks()))
    assert len(names) == len(set(names))


def test_a_remote_ui_reads_the_same_names_through_core(comms: Comms) -> None:
    from agent_comms.tool_caller import ThreadCaller
    from agent_comms.tools import CommsChannelNamesTool

    read = CommsChannelNamesTool().apply(comms, ThreadCaller("viewer"))
    assert read.names == comms.channels.catalog.read().channel_names()
