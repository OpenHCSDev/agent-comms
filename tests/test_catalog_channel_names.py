"""A catalog's channel names are the channels it builds, read without activity clocks."""

from agent_comms.activity_clocks import ActivityClocks
from agent_comms.comms import Comms


def test_channel_names_are_exactly_the_channels_views_builds(comms: Comms) -> None:
    catalog = comms.channels.catalog.read()
    names = catalog.channel_names()
    assert names == tuple(catalog.views(ActivityClocks()))
    assert len(names) == len(set(names))
