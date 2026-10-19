"""Explicit human Mark read never acknowledges a persistent agent's inbox."""

from agent_comms.channel_targets import CommandTarget
from agent_comms.read_basis import NamedViewer
import json

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.ui_model.unread import ReplyUnread
from native_unread_fixture import completed_snapshot


def answer(path, text):
    with path.open("a") as output:
        output.write(
            json.dumps({"type": "message", "timestamp": "2026-09-20T00:00:01Z", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
            + "\n"
        )


def test_mark_read_clears_thread_dm_and_channel_views_without_agent_delivery(tmp_path):
    comms = wire(tmp_path)
    source = tmp_path / "sender.jsonl"
    source.touch()
    answer(source, "Saved reply")
    comms.registry.declare(comms.registry.new_thread('sender', frozenset({'ci'}), str(tmp_path), session_file=str(source)))
    comms.registry.declare(comms.registry.new_thread('receiver', frozenset({'ci'}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("sender", "receiver", "Agent inbox remains unread").message.message_id
    comms.messaging.send("sender", viewer, "Human DM").message.message_id
    comms.messaging.send("sender", "#ci", "Channel message").message.message_id

    before = completed_snapshot(comms, str(tmp_path))
    assert before.thread_unread["sender"] == ReplyUnread(1)
    assert before.unread["sender"] == 1
    assert before.channel_unread["#ci"] == 1
    assert comms.bus.pending_count("receiver", CommandTarget.named("sender")) == 1
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 1

    comms.views.mark_user_view_read("sender", worktree=str(tmp_path))
    after_dm = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert after_dm.thread_unread["sender"] == ReplyUnread(0)
    assert after_dm.unread.get("sender", 0) == 0
    assert after_dm.channel_unread["#ci"] == 1
    assert comms.bus.pending_count("receiver", CommandTarget.named("sender")) == 1

    comms.views.mark_user_view_read("#ci", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 1

    comms.messaging.send("sender", "#ci", "One more channel message").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] > 0
    comms.views.mark_user_view_read("#any", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 2

    comms.messaging.send("receiver", viewer, "Human DM from a peer with no transcript").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).unread["receiver"] == 1
    comms.views.mark_user_view_read("receiver", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).unread.get("receiver", 0) == 0

    answer(source, "New reply after manual read")
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["sender"] == ReplyUnread(1)
    with pytest.raises(ValueError, match="registered"):
        comms.views.mark_user_view_read("missing", worktree=str(tmp_path))


def test_sessionless_cli_painted_tail_and_explicit_inbox_read_share_bus_marks(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        'cli', frozenset(), str(tmp_path), execution=ExternalThreadExecution,
    ))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send('cli', viewer, 'Painted CLI reply').message.message_id
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 1
    comms.views.mark_thread_view_read('cli', worktree=str(tmp_path),
        through=comms.transcripts.transcript_checkpoint('cli'))
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 0
    comms.messaging.send('cli', viewer, 'Explicitly marked CLI reply').message.message_id
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 1
    comms.views.mark_user_view_read('cli', worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 0


def bytes_read() -> int:
    """Bytes this process has asked the kernel to read so far."""
    with open("/proc/self/io") as counters:
        return int(next(line for line in counters if line.startswith("rchar:")).split()[1])


def test_mark_read_reads_the_marked_messages_not_the_whole_log(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset({'ci'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('archivist', frozenset({'bulk'}), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for _ in range(20):
        comms.messaging.send("archivist", "#bulk", "x" * 200_000).message.message_id
    comms.messaging.send("sender", viewer, "Human DM").message.message_id
    comms.messaging.send("sender", "#ci", "Channel message").message.message_id
    # An open UI has already counted unread messages and shown a page.
    comms.views.viewer_snapshot(str(tmp_path))
    comms.views.presentation.channel_page("#ci", viewer=NamedViewer(viewer))
    comms.messaging.send("sender", viewer, "Later human DM").message.message_id
    comms.messaging.send("sender", "#ci", "Later channel message").message.message_id
    log_size = comms.bus.log.path.stat().st_size
    assert log_size > 4_000_000

    for target in ("sender", "#ci"):
        before = bytes_read()
        comms.views.mark_user_view_read(target, worktree=str(tmp_path))
        assert bytes_read() - before < log_size // 4, target

    after = comms.views.viewer_snapshot(str(tmp_path))
    assert after.unread.get("sender", 0) == 0
    assert after.channel_unread["#ci"] == 0
    assert after.channel_unread["#bulk"] == 20
