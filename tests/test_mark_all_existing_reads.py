"""Whole-bus marks must also cover conversations that already have read evidence."""
from agent_comms.channel_targets import ChannelTarget
from dataclasses import replace

from agent_comms.read_basis import NamedViewer
from test_read_ledger import prepared


def test_mark_all_after_partial_reads_preserves_next_unread_message(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    first = comms.messaging.send('alice', '#team', 'already painted').message
    comms.messaging.send('bob', '#team', 'not painted').message
    page = comms.views.channel_display_page('#team', viewer=NamedViewer(viewer))
    selection = page.selection
    scope = selection.scope
    page = replace(page, selection=replace(selection, scope=replace(
        scope, displayed=scope.displayed.select({first.seq}))))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).unread[ChannelTarget('#team')].count == 1
    comms.views.mark_user_view_read('#any', worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).unread[ChannelTarget('#team')].count == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).unread[ChannelTarget('#any')].count == 0
    comms.messaging.send('alice', '#team', 'after the whole-bus cut')
    counts = {reference.name: shown.count for reference, shown in comms.views.viewer_snapshot(str(tmp_path)).unread.items() if isinstance(reference, ChannelTarget)}
    assert counts['#team'] == counts['#any'] == 1
