"""Sidebar states answer the same questions before and after a real read."""

import pytest

from agent_comms.display_order import NameChannelSort
from agent_comms.ui_model.sidebar import FailedSidebar, SidebarModel, WaitingSidebar
from agent_comms.ui_model.unread import ExactUnread, UnobservedUnread


def test_waiting_and_failed_sidebar_answer_without_a_snapshot():
    failure = ValueError("sidebar source could not be read")
    for observation in (WaitingSidebar(), FailedSidebar(failure)):
        assert observation.channel_order() is NameChannelSort
        assert observation.read_marker_notice() == ""
        assert observation.channel_unread("#any") == UnobservedUnread()
        assert observation.bus_unread("person") == UnobservedUnread()
    with pytest.raises(RuntimeError):
        WaitingSidebar().require_snapshot()
    with pytest.raises(ValueError) as caught:
        FailedSidebar(failure).require_snapshot()
    assert caught.value is failure


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
