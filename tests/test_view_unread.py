import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread


def test_channel_view_counts_are_not_agent_delivery_counts(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("sender", frozenset({"ci"}), str(tmp_path)))
    comms.registry.declare(Thread("receiver", frozenset({"ci", "docs"}), str(tmp_path)))
    comms.messaging.send("sender", "#ci", "first")
    comms.messaging.acknowledge("receiver")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 1
    comms.views.mark_channel_view_read("#ci", worktree=str(tmp_path))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    comms.messaging.send("sender", "#ci", "second")
    comms.messaging.send("sender", "#docs", "docs")
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    assert snapshot.channel_unread["#ci"] == snapshot.channel_unread["#docs"] == 1
    comms.views.mark_channel_view_read("#ci", worktree=str(tmp_path))
    assert comms.bus.pending_count("receiver", "#ci") == 1
    comms.messaging.send_user_message("#ci", "my own message", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#ci"] == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#docs"] == 1
    comms.messaging.send("sender", "receiver", "agent-to-agent DM in the aggregate feed")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 2
    comms.views.mark_channel_view_read("#any", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#docs"] == 0
    with patch.object(comms.bus.log, '_iter_log_unlocked', side_effect=AssertionError("idle rescan")):
        comms.views.viewer_snapshot(str(tmp_path))


def test_any_mode_expansion_keeps_unpainted_dm_unread(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.messaging.send("alice", "bob", "hidden until any-mode")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 0
    comms.views.mark_channel_view_read("#team", worktree=str(tmp_path))
    comms.channels.set_channel_any_mode("#team", True)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1


def test_any_mode_ack_covers_only_the_painted_participant_basis(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("carol", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("dave", frozenset(), str(tmp_path)))
    comms.channels.set_channel_any_mode("#team", True)
    comms.messaging.send_message("carol", "dave", "unpainted older DM")
    comms.messaging.send_message("alice", "bob", "painted DM")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    comms.views.mark_channel_view_read("#team", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 0

    # Carol's old DM becomes visible only after joining the channel. The
    # previous painted basis cannot acknowledge this newly included history.
    comms.channels.update_tags("carol", add=frozenset({"team"}))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] >= 1


def test_any_mode_rejects_stale_painted_page_after_participant_joins(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("carol", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("dave", frozenset(), str(tmp_path)))
    comms.channels.set_channel_any_mode("#team", True)
    comms.messaging.send_message("carol", "dave", "hidden old DM")
    comms.messaging.send_message("alice", "bob", "painted DM")
    painted = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    assert [message.seq for message in painted.messages] == [2]

    comms.channels.update_tags("carol", add=frozenset({"team"}))
    unread_after_join = wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"]
    with pytest.raises(ValueError, match="display.*changed"):
        comms.views.mark_channel_view_read(
            "#team",
            worktree=str(tmp_path),
            through=painted.newest_seq,
            expected_scope=painted.display_scope,
        )
    assert (
        wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == unread_after_join
    )


def test_any_mode_mark_keeps_captured_basis_if_registry_changes_during_write(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("carol", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("dave", frozenset(), str(tmp_path)))
    comms.channels.set_channel_any_mode("#team", True)
    comms.messaging.send_message("carol", "dave", "hidden old DM")
    comms.messaging.send_message("alice", "bob", "painted DM")
    painted = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    assert [message.seq for message in painted.messages] == [2]

    original_mark = comms.bus.reads.mark_displayed
    joined = []

    def join_before_marker(ledger, *args, **kwargs):
        carol = comms.registry.require("carol")
        comms.registry.register(replace(carol, tags=frozenset({"team"})))
        joined.append(True)
        original_mark(*args, **kwargs)

    monkeypatch.setattr(type(comms.bus.reads), "mark_displayed", join_before_marker)
    comms.views.mark_channel_view_read(
        "#team",
        worktree=str(tmp_path),
        through=painted.newest_seq,
        expected_scope=painted.display_scope,
    )
    assert joined
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] >= 1


@pytest.mark.skipif(os.name != "posix", reason="real /var/tmp crash durability")
def test_crashed_sender_reopen_does_not_hide_unpainted_any_mode_dm():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        root = Path(directory)
        comms = wire(root)
        comms.registry.declare(Thread("alice", frozenset({"team"}), str(root)))
        comms.registry.declare(Thread("bob", frozenset({"team"}), str(root)))
        comms.messaging.send_message("alice", "bob", "hidden before crash")
        comms.views.mark_channel_view_read("#team", worktree=str(root))
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, sys\nfrom agent_comms.comms import wire\nwire(sys.argv[1]).messaging.send_message('alice', 'bob', 'hidden before exit')\nos._exit(9)",
                str(root),
            ],
            check=False,
        )
        assert child.returncode == 9
        reopened = wire(root)
        reopened.channels.set_channel_any_mode("#team", True)
        assert [message.seq for message in reopened.views.full_history()] == [1, 2]
        assert reopened.views.viewer_snapshot(str(root)).channel_unread["#team"] == 2
