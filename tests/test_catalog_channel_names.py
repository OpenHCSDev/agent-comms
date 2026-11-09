"""A catalog's channel names are the channels it builds, read without activity clocks."""

from agent_comms.comms import Comms
from agent_comms.messaging.activity_clocks import ActivityClocks
from agent_comms.projects.checkouts import Checkout


def test_channel_names_are_exactly_the_channels_views_builds(comms: Comms) -> None:
    catalog = comms.channels.catalog.read()
    names = catalog.channel_names()
    assert names == tuple(catalog.views(ActivityClocks()))
    assert len(names) == len(set(names))


def test_reference_names_cross_the_wire_unchanged(comms: Comms) -> None:
    from agent_comms.messaging.display_references import ReferenceNames
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.transport.tool_caller import ThreadCaller
    from agent_comms.transport.tools.served import ServedCommsReferenceNamesTool

    comms.threads.claim_thread("viewer", tags=frozenset(), worktree=Checkout(comms.root))
    read = ServedCommsReferenceNamesTool().apply(comms, ThreadCaller("viewer"))
    assert FieldCodec.decode(ReferenceNames, FieldCodec.encode(read)) == read
    assert "viewer" in read.members
