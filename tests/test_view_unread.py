from agent_comms.channel_targets import CommandTarget
from agent_comms.read_basis import NamedViewer
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread
from agent_comms.private_path import FileRevision
from agent_comms.view_unread import ReplyIndex


def test_complete_reply_prefix_counts_during_growth_but_not_after_truncation(tmp_path):
    source = tmp_path / "native.jsonl"
    source.write_bytes(b"first reply\n")
    indexed = FileRevision.capture(source)
    index = ReplyIndex(indexed, indexed.size, 1, str(source), True)

    with source.open("ab") as stream:
        stream.write(b"another reply\n")
    growing = FileRevision.capture(source)
    assert index.covers(indexed)
    assert not index.covers(growing)  # Preparation still indexes the new records.
    assert index.counts(growing)      # Readers can show the held prefix now.
    assert index.counted_seen(growing.size, growing) == index.through
    assert index.counted_seen(indexed.size + 1, indexed) == 0

    source.write_bytes(b"")
    assert not index.counts(FileRevision.capture(source))


def test_channel_view_counts_are_not_agent_delivery_counts(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('sender', frozenset({'ci'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('receiver', frozenset({'ci', 'docs'}), str(tmp_path)))
    comms.messaging.send("sender", "#ci", "first").message.message_id
    messages = comms.bus.inbox("receiver")
    comms.messaging.acknowledge("receiver", tuple(message.reference for message in messages))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 1
    comms.views.mark_user_view_read("#ci", worktree=str(tmp_path))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    comms.messaging.send("sender", "#ci", "second").message.message_id
    comms.messaging.send("sender", "#docs", "docs").message.message_id
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    assert snapshot.channel_unread["#ci"] == snapshot.channel_unread["#docs"] == 1
    comms.views.mark_user_view_read("#ci", worktree=str(tmp_path))
    assert comms.bus.pending_count("receiver", CommandTarget.named("#ci")) == 1
    comms.messaging.send_user_message("#ci", "my own message", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#docs"] == 1
    comms.messaging.send("sender", "receiver", "agent-to-agent DM in the aggregate feed").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 2
    comms.views.mark_user_view_read("#any", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#docs"] == 0
    with patch.object(comms.bus.log, 'verified_snapshot', side_effect=AssertionError("idle rescan")):
        comms.views.viewer_snapshot(str(tmp_path))


def test_any_mode_expansion_keeps_unpainted_dm_unread(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path)))
    comms.messaging.send("alice", "bob", "hidden until any-mode").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 0
    comms.views.mark_user_view_read("#team", worktree=str(tmp_path))
    comms.channels.set_channel_any_mode("#team", True)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1


def test_any_mode_ack_covers_only_the_painted_participant_basis(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('carol', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('dave', frozenset(), str(tmp_path)))
    comms.channels.set_channel_any_mode("#team", True)
    comms.messaging.send("carol", "dave", "unpainted older DM").message
    comms.messaging.send("alice", "bob", "painted DM").message
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    comms.views.mark_user_view_read("#team", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 0

    # Carol's old DM becomes visible only after joining the channel. The
    # previous painted basis cannot acknowledge this newly included history.
    comms.channels.update_tags("carol", add=frozenset({"team"}))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] >= 1


def test_any_mode_rejects_stale_painted_page_after_participant_joins(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('carol', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('dave', frozenset(), str(tmp_path)))
    comms.channels.set_channel_any_mode("#team", True)
    comms.messaging.send("carol", "dave", "hidden old DM").message
    comms.messaging.send("alice", "bob", "painted DM").message
    painted = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    assert [message.seq for message in painted.messages] == [2]

    comms.channels.update_tags("carol", add=frozenset({"team"}))
    unread_after_join = wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"]
    with pytest.raises(ValueError, match="display.*changed"):
        comms.views.mark_page_read(painted, worktree=str(tmp_path))
    assert (
        wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == unread_after_join
    )



@pytest.mark.skipif(os.name != "posix", reason="real /var/tmp crash durability")
def test_crashed_sender_reopen_does_not_hide_unpainted_any_mode_dm():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        root = Path(directory)
        comms = wire(root)
        comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(root)))
        comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(root)))
        comms.messaging.send("alice", "bob", "hidden before crash").message
        comms.views.mark_user_view_read("#team", worktree=str(root))
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, sys\nfrom agent_comms.comms import wire\nwire(sys.argv[1]).messaging.send('alice', 'bob', 'hidden before exit')\nos._exit(9)",
                str(root),
            ],
            check=False,
        )
        assert child.returncode == 9
        reopened = wire(root)
        reopened.channels.set_channel_any_mode("#team", True)
        assert [message.seq for message in list(reopened.views.full_history_page().messages)] == [1, 2]
        assert reopened.views.viewer_snapshot(str(root)).channel_unread["#team"] == 2
