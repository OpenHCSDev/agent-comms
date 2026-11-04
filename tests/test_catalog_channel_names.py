"""A catalog's channel names are the channels it builds, read without activity clocks."""

from agent_comms.activity_clocks import ActivityClocks
from agent_comms.comms import Comms
from agent_comms.repo import Checkout


def test_channel_names_are_exactly_the_channels_views_builds(comms: Comms) -> None:
    catalog = comms.channels.catalog.read()
    names = catalog.channel_names()
    assert names == tuple(catalog.views(ActivityClocks()))
    assert len(names) == len(set(names))


def test_a_remote_ui_reads_the_snapshot_references_through_core(comms: Comms) -> None:
    from agent_comms.display_references import CurrentDisplayReferences
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tool_caller import ThreadCaller
    from agent_comms.tools import CommsReferenceNamesTool, ReferenceNames

    comms.threads.claim_thread("viewer", tags=frozenset(), worktree=Checkout(comms.root))
    read = CommsReferenceNamesTool().apply(comms, ThreadCaller("viewer"))
    # What crosses the wire decodes to the same names the sidebar snapshot links through.
    read = FieldCodec.decode(ReferenceNames, FieldCodec.encode(read))
    catalog = comms.channels.catalog.read()
    expected = CurrentDisplayReferences.from_registry("root", comms.registry.snapshot(), catalog.channel_names())
    assert read.references("root") == expected
    assert "viewer" in read.members and read.channels == catalog.channel_names()
