"""One complete map keeps native replies, DMs and channels distinct."""

from dataclasses import replace

import pytest

from agent_comms.channel_targets import ChannelTarget, NativeThreadTarget, ThreadTarget
from agent_comms.ui_model.sidebar import SidebarModel
from agent_comms.ui_model.unread import ExactUnread, ReplyUnread, UnreadPresentation
from native_unread_fixture import completed_snapshot
from test_thread_unread import append, setup_thread


def test_one_map_keeps_view_counts_and_read_positions_separate(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source, content="Native reply one")
    append(source, content="Native reply two")
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("worker", viewer, "Human DM")
    comms.messaging.send("worker", "#ci", "Channel message")
    before = completed_snapshot(comms, str(tmp_path))
    assert before.unread[NativeThreadTarget("worker")] == ReplyUnread(2)
    assert before.unread[ThreadTarget("worker")] == ExactUnread(1)
    assert before.unread[ChannelTarget("#ci")] == ExactUnread(1)
    channels, threads = SidebarModel.derive(before)
    assert channels["#ci"].unread is before.unread[ChannelTarget("#ci")]
    assert threads["worker"].unread is before.unread[NativeThreadTarget("worker")]
    for target in (NativeThreadTarget("missing"), ThreadTarget("missing"), ChannelTarget("missing")):
        with pytest.raises(KeyError, match="missing"):
            UnreadPresentation.for_view(before, target)
    same_name = replace(before, unread={ThreadTarget("same"): ExactUnread(2),
                                       ChannelTarget("same"): ExactUnread(7)})
    assert UnreadPresentation.for_view(same_name, ThreadTarget("same")) == ExactUnread(2)
    assert UnreadPresentation.for_view(same_name, ChannelTarget("same")) == ExactUnread(7)
    comms.views.mark_user_view_read("worker", worktree=str(tmp_path))
    comms.views.mark_user_view_read("#ci", worktree=str(tmp_path))
    after = completed_snapshot(comms, str(tmp_path))
    assert after.unread[NativeThreadTarget("worker")] == ReplyUnread(0)
    assert after.unread[ThreadTarget("worker")] == ExactUnread(0)
    assert after.unread[ChannelTarget("#ci")] == ExactUnread(0)


def test_sibling_views_convert_and_clear_only_their_painted_source(tmp_path):
    comms, source = setup_thread(tmp_path)
    human, saved = ThreadTarget("worker"), NativeThreadTarget("worker")
    for target in (human, saved):
        assert target.native() == saved
        assert target.external() == human
    append(source, content="Saved reply stays unread while reading the DM")
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("worker", viewer, "Human DM")
    before = completed_snapshot(comms, str(tmp_path))
    assert before.unread[human] == ExactUnread(1)
    assert before.unread[saved] == ReplyUnread(1)
    page = comms.views.dm_display_page("worker", worktree=str(tmp_path))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    after_human = completed_snapshot(comms, str(tmp_path))
    assert after_human.unread[human] == ExactUnread(0)
    assert after_human.unread[saved] == ReplyUnread(1)
    comms.messaging.send("worker", viewer, "Human DM stays unread while reading the saved reply")
    comms.views.mark_thread_view_read("worker", worktree=str(tmp_path),
                                    through=comms.transcripts.transcript_checkpoint("worker"))
    after_saved = completed_snapshot(comms, str(tmp_path))
    assert after_saved.unread[human] == ExactUnread(1)
    assert after_saved.unread[saved] == ReplyUnread(0)
