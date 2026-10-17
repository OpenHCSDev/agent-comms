"""Viewer-only presence flags reveal archived threads without reviving owners."""
from agent_comms.storage_rules import StorageRule

from agent_comms.comms import wire
from agent_comms.thread_identity import ThreadRole
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def test_stopped_and_archived_are_explicit_view_filters(tmp_path):
    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    for name in ("active", "stopped", "archived"):
        comms.registry.declare(Thread.create(name, frozenset({"team"}), str(tmp_path), storage_rule=StorageRule()))
    comms.owners.stop("stopped")
    comms.owners.stop("archived")
    comms.threads.archive("archived")
    comms.registry.declare(Thread.create("human", frozenset({"team"}), str(tmp_path), role=ThreadRole.USER, storage_rule=StorageRule()))

    def names(snapshot):
        return set(snapshot.threads)

    def members(snapshot):
        return snapshot.channels["#team"].members

    default = comms.views.viewer_snapshot(str(tmp_path))
    assert names(default) == {"active", "stopped"}
    assert set(members(default)) == {"active", "stopped"}
    assert default.show_stopped and not default.show_archived
    running = comms.views.viewer_snapshot(str(tmp_path), show_stopped=False)
    assert names(running) == set(members(running)) == {"active"}
    both = comms.views.viewer_snapshot(str(tmp_path), show_archived=True)
    assert names(both) == set(members(both)) == {"active", "stopped", "archived"}
    archived_only = comms.views.viewer_snapshot(str(tmp_path), show_stopped=False, show_archived=True)
    assert names(archived_only) == set(members(archived_only)) == {"active", "archived"}
    archived = archived_only.threads["archived"]
    assert archived.status == ArchivedThreadStatus()
    assert comms.registry.status("stopped") == StoppedThreadStatus()
    assert comms.registry.status("archived") == ArchivedThreadStatus()
    assert {view.thread.name for view in comms.views.thread_views()} == {"active", "stopped"}
    channel = next(view for view in comms.views.channel_views() if view.channel.name == "#team")
    assert set(channel.members) == {"active", "stopped"}
