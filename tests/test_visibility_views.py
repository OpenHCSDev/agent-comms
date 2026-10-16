"""Viewer-only presence flags reveal archived threads without reviving owners."""

import pytest

from agent_comms.comms import wire
from agent_comms.display_order import NameChannelSort
from agent_comms.presentation import ListingFilter
from agent_comms.ui_model.sidebar import SidebarModel
from agent_comms.ui_model.unread import UnobservedUnread
from agent_comms.thread_identity import ThreadRole
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def test_stopped_and_archived_are_explicit_view_filters(tmp_path):
    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    for name in ("active", "stopped", "archived"):
        comms.registry.declare(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.owners.stop("stopped")
    comms.owners.stop("archived")
    comms.threads.archive("archived")
    comms.registry.declare(Thread("human", frozenset({"team"}), str(tmp_path), role=ThreadRole.USER))

    def names(snapshot):
        return {view.thread.name for view in snapshot.threads}

    def members(snapshot):
        return next(view.members for view in snapshot.channels if view.channel.name == "#team")

    default = comms.views.viewer_snapshot(str(tmp_path))
    assert names(default) == {"active", "stopped"}
    assert set(members(default)) == {"active", "stopped"}
    assert default.filters == ListingFilter(show_stopped=True, show_archived=False)
    running = comms.views.viewer_snapshot(str(tmp_path), show_stopped=False)
    assert names(running) == set(members(running)) == {"active"}
    both = comms.views.viewer_snapshot(str(tmp_path), show_archived=True)
    assert names(both) == set(members(both)) == {"active", "stopped", "archived"}
    archived_only = comms.views.viewer_snapshot(str(tmp_path), show_stopped=False, show_archived=True)
    assert names(archived_only) == set(members(archived_only)) == {"active", "archived"}
    archived = next(view for view in archived_only.threads if view.thread.name == "archived")
    assert archived.status == ArchivedThreadStatus()
    assert comms.registry.status("stopped") == StoppedThreadStatus()
    assert comms.registry.status("archived") == ArchivedThreadStatus()
    assert {view.thread.name for view in comms.views.thread_views()} == {"active", "stopped"}
    channel = next(view for view in comms.views.channel_views() if view.channel.name == "#team")
    assert set(channel.members) == {"active", "stopped"}


def test_sidebar_observation_owns_unavailable_and_observed_answers(tmp_path):
    model = SidebarModel(lambda flush: flush())
    assert model.channel_order is NameChannelSort
    assert model.read_marker_notice is None
    assert isinstance(model.observation.unread_for(lambda snapshot: UnobservedUnread()), UnobservedUnread)
    with pytest.raises(RuntimeError, match='has not been observed'):
        _ = model.filters

    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(Thread('member', frozenset({'team'}), str(tmp_path)))
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    model.apply(snapshot, model.derive(snapshot))
    assert model.filters is snapshot.filters
    assert model.observation.require_snapshot() is snapshot
    assert model.channels.rows['#team'].members == ('member',)
    for target in ('#all', '#any', '#none'):
        assert model.channels.rows[target].tooltip.endswith(f'tags: {target}')

    failure = ValueError('Sidebar read failed')
    model.clear(failure)
    assert model.threads.rows == model.channels.rows == {}
    assert isinstance(model.observation.unread_for(lambda snapshot: UnobservedUnread()), UnobservedUnread)
    with pytest.raises(ValueError) as refused:
        _ = model.filters
    assert refused.value is failure
