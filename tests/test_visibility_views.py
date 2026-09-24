"""Viewer-only presence flags reveal archived threads without reviving owners."""

from agent_comms import Thread, ThreadRole, ThreadStatus, wire


def test_stopped_and_archived_are_explicit_view_filters(tmp_path):
    comms = wire(tmp_path)
    for name in ("active", "stopped", "archived"):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.stop("stopped")
    comms.stop("archived")
    comms.archive("archived")
    comms.register(Thread("human", frozenset({"team"}), str(tmp_path), role=ThreadRole.USER))

    def names(snapshot):
        return {view.thread.name for view in snapshot.threads}

    def members(snapshot):
        return next(view.members for view in snapshot.channels if view.channel.name == "#team")

    default = comms.viewer_snapshot(str(tmp_path))
    assert names(default) == {"active", "stopped"}
    assert set(members(default)) == {"active", "stopped"}
    assert default.show_stopped and not default.show_archived
    running = comms.viewer_snapshot(str(tmp_path), show_stopped=False)
    assert names(running) == set(members(running)) == {"active"}
    both = comms.viewer_snapshot(str(tmp_path), show_archived=True)
    assert names(both) == set(members(both)) == {"active", "stopped", "archived"}
    archived_only = comms.viewer_snapshot(str(tmp_path), show_stopped=False, show_archived=True)
    assert names(archived_only) == set(members(archived_only)) == {"active", "archived"}
    archived = next(view for view in archived_only.threads if view.thread.name == "archived")
    assert archived.status is ThreadStatus.ARCHIVED
    assert comms.registry.status("stopped") is ThreadStatus.STOPPED
    assert comms.registry.status("archived") is ThreadStatus.ARCHIVED
    assert {view.thread.name for view in comms.thread_views()} == {"active", "stopped"}
    channel = next(view for view in comms.channel_views() if view.channel.name == "#team")
    assert set(channel.members) == {"active", "stopped"}
