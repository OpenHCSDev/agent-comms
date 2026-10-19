"""Whole-bus marks must also cover conversations that already have read evidence."""
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
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread['#team'] == 1
    comms.views.mark_user_view_read('#any', worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread['#team'] == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread['#any'] == 0
    comms.messaging.send('alice', '#team', 'after the whole-bus cut')
    counts = comms.views.viewer_snapshot(str(tmp_path)).channel_unread
    assert counts['#team'] == counts['#any'] == 1
