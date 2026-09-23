from unittest.mock import patch

from agent_comms import Thread, wire


def test_channel_view_counts_are_not_agent_delivery_counts(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("sender", frozenset({"ci"}), str(tmp_path)))
    comms.register(Thread("receiver", frozenset({"ci", "docs"}), str(tmp_path)))
    comms.send("sender", "#ci", "first")
    comms.acknowledge("receiver")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 1
    comms.mark_channel_view_read("#ci", worktree=str(tmp_path))
    assert wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    comms.send("sender", "#ci", "second")
    comms.send("sender", "#docs", "docs")
    snapshot = comms.viewer_snapshot(str(tmp_path))
    assert snapshot.channel_unread["#ci"] == snapshot.channel_unread["#docs"] == 1
    comms.mark_channel_view_read("#ci", worktree=str(tmp_path))
    assert comms.pending_count("receiver", "#ci") == 1
    comms.send_user_message("#ci", "my own message", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#docs"] == 1
    comms.send("sender", "receiver", "agent-to-agent DM in the aggregate feed")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 4
    comms.mark_channel_view_read("#any", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#docs"] == 1
    with patch.object(comms.bus, "_iter_log_unlocked", side_effect=AssertionError("idle rescan")):
        comms.viewer_snapshot(str(tmp_path))
