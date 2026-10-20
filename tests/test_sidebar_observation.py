"""Sidebar states answer the same questions before and after a real read."""

import pytest

from agent_comms.ui_model.sidebar import SidebarModel
from agent_comms.ui_model.unread import ExactUnread, UnobservedUnread




def test_sidebar_apply_and_failure_publish_their_original_answers(comms):
    scheduled = []
    model = SidebarModel(scheduled.append)
    assert model.observation.channel_unread("#any") == UnobservedUnread()
    snapshot = comms.views.viewer_snapshot(str(comms.root.parent))
    model.apply(snapshot, SidebarModel.derive(snapshot))
    assert model.observation.require_snapshot() is snapshot
    assert model.observation.channel_order() is snapshot.channel_order
    assert model.observation.channel_unread("#any") == ExactUnread(0)
    assert model.observation.bus_unread("missing") == ExactUnread(0)
    assert set(model.channels.rows) == set(snapshot.channels)
    failure = ValueError("original sidebar read failure")
    model.clear(failure)
    assert not model.channels.rows and not model.threads.rows
    assert model.observation.channel_unread("#any") == UnobservedUnread()
    with pytest.raises(ValueError) as caught:
        model.observation.require_snapshot()
    assert caught.value is failure
