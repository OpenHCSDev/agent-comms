"""Viewer-only presence flags reveal archived threads without reviving owners."""

from agent_comms.comms import wire
from agent_comms.thread_identity import ThreadRole
from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def test_channel_archive_hides_rows_without_retiring_open_members_or_history(tmp_path):
    from agent_comms.cli_commands import ArchiveChannelCliCommand, RestoreChannelCliCommand

    comms = wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(Thread('member', frozenset({'team', 'other'}), str(tmp_path)))
    comms.registry.declare(Thread('sender', frozenset({'other'}), str(tmp_path)))
    comms.messaging.send('sender', '#team', 'original history')
    registry = comms.registry.snapshot()
    original_wire = comms.bus.log.path.read_bytes()
    archived = ArchiveChannelCliCommand.execute_target(comms, '#team', {}, confirmed=True)
    assert archived.name == '#team' and archived.archived
    assert comms.registry.snapshot() == registry
    assert comms.bus.log.path.read_bytes() == original_wire
    assert comms.channels.catalog.read().resolve('#team') == archived
    assert '#team' in comms.channels.channels()
    current = comms.views.viewer_snapshot(str(tmp_path))
    assert '#team' not in {view.channel.name for view in current.visible_channels}
    assert '#team' in {view.channel.name for view in current.channels}
    assert {view.thread.name for view in current.threads} == {'member', 'sender'}
    assert next(view for view in current.channels if view.channel.name == '#team').members == ('member',)
    assert 'member' in {candidate.name for candidate in current.mention_candidates('#team')}
    visible = comms.views.viewer_snapshot(str(tmp_path), show_archived=True)
    selected = next(view for view in visible.visible_channels if view.channel.name == '#team')
    assert selected.channel.archived and selected.members == ('member',)
    assert RestoreChannelCliCommand.bindings(comms, '#team')
    assert not ArchiveChannelCliCommand.bindings(comms, '#team')
    comms.messaging.send('sender', '#team', 'still routable')
    assert [message.body for message in comms.bus.inbox('member')] == ['original history', 'still routable']
    before_restore = comms.registry.snapshot()
    restored = RestoreChannelCliCommand.execute_target(comms, '#team', {})
    assert not restored.archived
    assert '#team' in {view.channel.name for view in comms.views.viewer_snapshot(str(tmp_path)).visible_channels}
    assert comms.registry.snapshot() == before_restore


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
    assert default.show_stopped and not default.show_archived
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
