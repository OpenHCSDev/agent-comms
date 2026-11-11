"""Explicit human Mark read never acknowledges a persistent agent's inbox."""

import json

import pytest

from agent_comms.comms import wire
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.read_basis import NamedViewer
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.ui_model.unread import ReplyUnread
from conftest import installed_comms
from native_unread_fixture import completed_snapshot


def answer(path, text):
    with path.open("a") as output:
        output.write(
            json.dumps({"type": "message", "timestamp": "2026-09-20T00:00:01Z", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
            + "\n"
        )


def test_mark_read_clears_thread_dm_and_channel_views_without_agent_delivery(tmp_path):
    comms = installed_comms(tmp_path)
    here = Checkout(tmp_path)
    source = tmp_path / "sender.jsonl"
    source.touch()
    answer(source, "Saved reply")
    comms.registry.declare(NativeThread('sender', frozenset({'ci'}), here, session_file=RegistrySessionLocation.from_path(str(source), comms.root)))
    comms.registry.declare(NativeThread('receiver', frozenset({'ci'}), here))
    viewer = comms.messaging.user_identity(here).name
    comms.messaging.send("sender", "receiver", "Agent inbox remains unread")
    comms.messaging.send("sender", viewer, "Human DM")
    comms.messaging.send("sender", "#ci", "Channel message")

    before = completed_snapshot(comms, here)
    assert before.thread_unread["sender"] == ReplyUnread(1)
    assert before.unread["sender"].count == 1
    assert before.channel_unread["#ci"].count == 1
    assert comms.bus.pending_count("receiver", CommandTarget.named("sender")) == 1
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 1

    comms.views.mark_user_view_read("sender", worktree=here)
    after_dm = wire(tmp_path).views.viewer_snapshot(here)
    assert after_dm.thread_unread["sender"] == ReplyUnread(0)
    assert after_dm.unread["sender"].count == 0
    assert after_dm.channel_unread["#ci"].count == 1
    assert comms.bus.pending_count("receiver", CommandTarget.named("sender")) == 1

    comms.views.mark_user_view_read("#ci", worktree=here)
    assert comms.views.viewer_snapshot(here).channel_unread["#ci"].count == 0
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 1

    comms.messaging.send("sender", "#ci", "One more channel message")
    assert comms.views.viewer_snapshot(here).channel_unread["#any"].count > 0
    comms.views.mark_user_view_read("#any", worktree=here)
    assert comms.views.viewer_snapshot(here).channel_unread["#any"].count == 0
    assert comms.views.viewer_snapshot(here).channel_unread["#ci"].count == 0
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 2

    comms.messaging.send("receiver", viewer, "Human DM from a peer with no transcript")
    assert comms.views.viewer_snapshot(here).unread["receiver"].count == 1
    comms.views.mark_user_view_read("receiver", worktree=here)
    assert comms.views.viewer_snapshot(here).unread["receiver"].count == 0

    answer(source, "New reply after manual read")
    assert completed_snapshot(comms, here).thread_unread["sender"] == ReplyUnread(1)
    with pytest.raises(ValueError):
        comms.views.mark_user_view_read("missing", worktree=here)


def test_sessionless_cli_painted_tail_and_explicit_inbox_read_share_bus_marks(tmp_path):
    comms = installed_comms(tmp_path)
    here = Checkout(tmp_path)
    comms.registry.declare(ExternalThread(
        'cli', frozenset(), here))
    viewer = comms.messaging.user_identity(here).name
    comms.messaging.send('cli', viewer, 'Painted CLI reply')
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 1
    comms.views.mark_thread_view_read('cli', worktree=here,
        through=comms.transcripts.transcript_checkpoint('cli'))
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 0
    comms.messaging.send('cli', viewer, 'Explicitly marked CLI reply')
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 1
    comms.views.mark_user_view_read('cli', worktree=here)
    assert comms.bus.pending_count(viewer, CommandTarget.named('cli')) == 0


def bytes_read() -> int:
    """Bytes this process has asked the kernel to read so far."""
    with open("/proc/self/io") as counters:
        return int(next(line for line in counters if line.startswith("rchar:")).split()[1])


def test_mark_read_reads_the_marked_messages_not_the_whole_log(tmp_path):
    comms = installed_comms(tmp_path)
    here = Checkout(tmp_path)
    comms.registry.declare(NativeThread('sender', frozenset({'ci'}), here))
    comms.registry.declare(NativeThread('archivist', frozenset({'bulk'}), here))
    viewer = comms.messaging.user_identity(here).name
    for _ in range(20):
        comms.messaging.send("archivist", "#bulk", "x" * 200_000)
    comms.messaging.send("sender", viewer, "Human DM")
    comms.messaging.send("sender", "#ci", "Channel message")
    # An open UI has already counted unread messages and shown a page.
    comms.views.viewer_snapshot(here)
    comms.views.presentation.channel_page("#ci", viewer=NamedViewer(viewer))
    comms.messaging.send("sender", viewer, "Later human DM")
    comms.messaging.send("sender", "#ci", "Later channel message")
    bus = comms.bus.bus
    # The log is the bus file and its WAL: recent writes live in the WAL until a checkpoint.
    log_size = sum(path.stat().st_size for path in (bus.path, type(bus).wal_path(bus.path))
                   if path.exists())
    assert log_size > 4_000_000

    for target in ("sender", "#ci"):
        before = bytes_read()
        comms.views.mark_user_view_read(target, worktree=here)
        assert bytes_read() - before < log_size // 4, target

    after = comms.views.viewer_snapshot(here)
    assert after.unread["sender"].count == 0
    assert after.channel_unread["#ci"].count == 0
    assert after.channel_unread["#bulk"].count == 20
