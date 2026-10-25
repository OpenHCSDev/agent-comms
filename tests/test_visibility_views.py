"""Viewer-only presence flags reveal archived threads without reviving owners."""


from agent_comms.repo import Checkout
import pytest

from agent_comms.comms import wire
from agent_comms.registry_document import RosterVisibility
from agent_comms.thread_identity import UserThreadRole
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


@pytest.mark.parametrize(("visibility", "shown"), [
    (RosterVisibility(), {"active", "stopped"}),
    (RosterVisibility(stopped=False), {"active"}),
    (RosterVisibility(archived=True), {"active", "stopped", "archived"}),
    (RosterVisibility(stopped=False, archived=True), {"active", "archived"}),
])
def test_roster_visibility_selects_threads_and_channel_members(tmp_path, visibility, shown):
    comms = wire(tmp_path)
    for name in ("active", "stopped", "archived"):
        comms.registry.declare(Thread(name, frozenset({'team'}), Checkout(tmp_path)))
    comms.owners.stop("stopped")
    comms.owners.stop("archived")
    comms.threads.archive("archived")
    comms.registry.declare(Thread('human', frozenset({'team'}), Checkout(tmp_path), role=UserThreadRole))
    assert comms.registry.status("stopped") == StoppedThreadStatus()
    assert comms.registry.status("archived") == ArchivedThreadStatus()

    snapshot = comms.views.viewer_snapshot(str(tmp_path), visibility)
    assert set(snapshot.threads) == set(snapshot.channels["#team"].members) == shown
    assert {view.thread.name for view in comms.views.thread_views(visibility)} == shown
    channel, = (view for view in comms.views.channel_views(visibility) if view.channel.name == "#team")
    assert set(channel.members) == shown
