from agent_comms.coordination_errors import DMPaintedBoundChanged
from agent_comms.read_basis import NamedViewer
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.private_path import FileRevision
from agent_comms.view_unread import Counted, ReplyIndex


def test_reply_suffix_counts_only_its_revision_or_a_fully_read_source(tmp_path):
    source = tmp_path / "native.jsonl"
    source.write_bytes(b"first reply\n")
    indexed = FileRevision.capture(source)
    index = ReplyIndex(revision=indexed, records=1, source=str(source), start=0)

    with source.open("ab") as stream:
        stream.write(b"another reply\n")
    growing = FileRevision.capture(source)
    assert index.counts(indexed, 0)
    assert not index.counts(growing, 0)  # A changed source needs a fresh suffix.
    assert index.counts(growing, growing.size)  # The read mark itself proves zero unread.
    assert index.counted(1, 0) == Counted(1)

    source.write_bytes(b"")
    truncated = FileRevision.capture(source)
    assert not index.continues(truncated)
    assert index.counts(truncated, 0)  # Empty and fully read, independently of the old index.


def test_any_mode_expansion_keeps_unpainted_dm_unread(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path)))
    comms.messaging.send("alice", "bob", "hidden until any-mode").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 0
    comms.views.mark_user_view_read("#team", worktree=str(tmp_path))
    comms.channels.set_channel_any_mode("#team", True)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 1
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 1


def test_any_mode_ack_covers_only_the_painted_participant_basis(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('carol', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('dave', frozenset(), str(tmp_path)))
    comms.channels.set_channel_any_mode("#team", True)
    comms.messaging.send("carol", "dave", "unpainted older DM").message
    comms.messaging.send("alice", "bob", "painted DM").message
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 1
    comms.views.mark_user_view_read("#team", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 0

    # Carol's old DM becomes visible only after joining the channel. The
    # previous painted basis cannot acknowledge this newly included history.
    comms.channels.update_tags("carol", add=frozenset({"team"}))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count >= 1


def test_any_mode_rejects_stale_painted_page_after_participant_joins(tmp_path):
    comms = installed_comms(tmp_path)
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
    unread_after_join = wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count
    with pytest.raises(DMPaintedBoundChanged):
        comms.views.mark_page_read(painted, worktree=str(tmp_path))
    assert (
        wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == unread_after_join
    )


@pytest.mark.skipif(os.name != "posix", reason="real /var/tmp crash durability")
def test_crashed_sender_reopen_does_not_hide_unpainted_any_mode_dm():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        root = Path(directory)
        comms = installed_comms(root)
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
        assert [message.seq for message in list(reopened.views.channel_display_page("#team").messages)] == [1, 2]
        assert reopened.views.viewer_snapshot(str(root)).channel_unread["#team"].count == 2
