"""Each sidebar count is either indexing or an exact value, never an absent zero."""

import pytest

from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread
from agent_comms.ui_model.sidebar import ObservedSidebar, SidebarModel
from agent_comms.ui_model.unread import ExactUnread, IndexingUnread, UnreadPresentation
from conftest import installed_comms


def test_indexing_roster_and_exact_count_share_required_maps(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(ExternalThread(
        "peer", frozenset(), Checkout(tmp_path / "peer")))
    archived_peer = ExternalThread("archived-peer", frozenset(), Checkout(tmp_path / "archived"))
    comms.registry.declare(archived_peer)
    comms.registry.archive("archived-peer")
    comms.channels.create_tag("old")
    comms.channels.set_channel_archived("#old", True)
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    roster = comms.views.presentation.roster(viewer)
    assert isinstance(roster.unread["peer"], IndexingUnread)
    assert isinstance(roster.unread["archived-peer"], IndexingUnread)
    assert isinstance(roster.channel_unread["#old"], IndexingUnread)
    assert isinstance(ObservedSidebar(roster).peer_unread(archived_peer.incarnation), IndexingUnread)
    assert isinstance(ObservedSidebar(roster).channel_unread("#old"), IndexingUnread)
    assert all(isinstance(value, IndexingUnread) for value in roster.channel_unread.values())
    assert roster.has_indexing_unread()
    with pytest.raises(KeyError):
        UnreadPresentation.for_bus(roster, "missing")
    with pytest.raises(KeyError):
        UnreadPresentation.for_channel(roster, "#missing")

    before = comms.views.viewer_snapshot(Checkout(tmp_path))
    assert before.unread["peer"] == ExactUnread(0)
    assert before.unread["archived-peer"] == ExactUnread(0)
    assert before.channel_unread["#any"] == ExactUnread(0)
    assert before.channel_unread["#old"] == ExactUnread(0)
    assert set(roster.unread) == set(before.unread)
    assert set(roster.channel_unread) == set(before.channel_unread)
    assert not before.has_indexing_unread()
    comms.messaging.send("peer", viewer, "a fresh private message")
    after = comms.views.viewer_snapshot(Checkout(tmp_path))
    assert after.unread["peer"] == ExactUnread(1)
    assert not after.has_indexing_unread()
    channels, threads = SidebarModel.derive(after)
    assert threads["peer"].unread == after.unread["peer"]
    assert channels["#any"].unread == after.channel_unread["#any"]
