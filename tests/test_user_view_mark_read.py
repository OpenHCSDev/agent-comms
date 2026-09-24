"""Explicit human Mark read never acknowledges a persistent agent's inbox."""

import json

import pytest

from agent_comms import Thread, wire


def answer(path, text):
    with path.open("a") as output:
        output.write(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": text}})
            + "\n"
        )


def test_mark_read_clears_thread_dm_and_channel_views_without_agent_delivery(tmp_path):
    comms = wire(tmp_path)
    source = tmp_path / "sender.jsonl"
    source.touch()
    answer(source, "Saved reply")
    comms.register(Thread("sender", frozenset({"ci"}), str(tmp_path), session_file=str(source)))
    comms.register(Thread("receiver", frozenset({"ci"}), str(tmp_path)))
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("sender", "receiver", "Agent inbox remains unread")
    comms.send("sender", viewer, "Human DM")
    comms.send("sender", "#ci", "Channel message")

    before = comms.viewer_snapshot(str(tmp_path))
    assert before.thread_unread["sender"] == before.unread["sender"] == 1
    assert before.channel_unread["#ci"] == 1
    assert comms.pending_count("receiver", "sender") == 1
    assert comms.pending_count("receiver", "#ci") == 1

    comms.mark_user_view_read("sender", worktree=str(tmp_path))
    after_dm = wire(tmp_path).viewer_snapshot(str(tmp_path))
    assert after_dm.thread_unread["sender"] == after_dm.unread.get("sender", 0) == 0
    assert after_dm.channel_unread["#ci"] == 1
    assert comms.pending_count("receiver", "sender") == 1

    comms.mark_user_view_read("#ci", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    assert comms.pending_count("receiver", "#ci") == 1

    comms.send("sender", "#ci", "One more channel message")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#any"] > 0
    comms.mark_user_view_read("#any", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 1
    assert comms.pending_count("receiver", "#ci") == 2

    comms.send("receiver", viewer, "Human DM from a peer with no transcript")
    assert comms.viewer_snapshot(str(tmp_path)).unread["receiver"] == 1
    comms.mark_user_view_read("receiver", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).unread.get("receiver", 0) == 0

    answer(source, "New reply after manual read")
    assert comms.viewer_snapshot(str(tmp_path)).thread_unread["sender"] == 1
    with pytest.raises(ValueError, match="registered"):
        comms.mark_user_view_read("missing", worktree=str(tmp_path))
