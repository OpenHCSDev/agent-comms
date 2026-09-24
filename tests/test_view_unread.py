import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_any_mode_expansion_keeps_unpainted_dm_unread(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.send("alice", "bob", "hidden until any-mode")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 0
    comms.mark_channel_view_read("#team", worktree=str(tmp_path))
    comms.set_channel_any_mode("#team", True)
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    assert wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1


def test_any_mode_ack_covers_only_the_painted_participant_basis(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("carol", frozenset(), str(tmp_path)))
    comms.register(Thread("dave", frozenset(), str(tmp_path)))
    comms.set_channel_any_mode("#team", True)
    comms.send_message("carol", "dave", "unpainted older DM")
    comms.send_message("alice", "bob", "painted DM")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    comms.mark_channel_view_read("#team", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 0

    # Carol's old DM becomes visible only after joining the channel. The
    # previous painted basis cannot acknowledge this newly included history.
    comms.update_tags("carol", add=frozenset({"team"}))
    assert wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#team"] >= 1


def test_any_mode_rejects_stale_painted_page_after_participant_joins(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("bob", frozenset({"team"}), str(tmp_path)))
    comms.register(Thread("carol", frozenset(), str(tmp_path)))
    comms.register(Thread("dave", frozenset(), str(tmp_path)))
    comms.set_channel_any_mode("#team", True)
    comms.send_message("carol", "dave", "hidden old DM")
    comms.send_message("alice", "bob", "painted DM")
    painted = comms.channel_display_page("#team", worktree=str(tmp_path))
    assert [message.seq for message in painted.messages] == [2]

    comms.update_tags("carol", add=frozenset({"team"}))
    unread_after_join = wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#team"]
    with pytest.raises(ValueError, match="display.*changed"):
        comms.mark_channel_view_read(
            "#team",
            worktree=str(tmp_path),
            through=painted.newest_seq,
            expected_scope=painted.display_scope,
        )
    assert (
        wire(tmp_path).viewer_snapshot(str(tmp_path)).channel_unread["#team"] == unread_after_join
    )


def test_v1_exact_channel_marker_resets_with_notice(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("alice", frozenset({"team"}), str(tmp_path)))
    viewer = comms.user_identity(str(tmp_path)).name
    message = comms.send_message("alice", "#team", "message not proven painted")
    marker = tmp_path / "read_markers.json"
    marker.write_text(json.dumps({comms.bus._marker_key(viewer, "#team"): message.seq}))

    snapshot = wire(tmp_path).viewer_snapshot(str(tmp_path))
    assert snapshot.channel_unread["#team"] == 1
    assert snapshot.read_marker_notice


@pytest.mark.skipif(os.name != "posix", reason="real /var/tmp crash durability")
def test_crashed_sender_reopen_does_not_hide_unpainted_any_mode_dm():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        root = Path(directory)
        comms = wire(root)
        comms.register(Thread("alice", frozenset({"team"}), str(root)))
        comms.register(Thread("bob", frozenset({"team"}), str(root)))
        comms.send_message("alice", "bob", "hidden before crash")
        comms.mark_channel_view_read("#team", worktree=str(root))
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from agent_comms import wire; "
                "wire(sys.argv[1]).send_message('alice','bob','hidden before exit'); "
                "os._exit(9)",
                str(root),
            ],
            check=False,
        )
        assert child.returncode == 9
        reopened = wire(root)
        reopened.set_channel_any_mode("#team", True)
        assert [message.seq for message in reopened.full_history()] == [1, 2]
        assert reopened.viewer_snapshot(str(root)).channel_unread["#team"] == 2
