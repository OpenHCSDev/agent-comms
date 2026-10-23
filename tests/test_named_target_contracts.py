"""Named destinations share counting, change attribution and human read contracts."""

import pytest

from agent_comms.channel_targets import CommandTarget, NamedTarget
from agent_comms.thread_execution import ExternalThreadExecution
from bus_record_fixture import context_comms


@pytest.fixture
def named_target_journey(tmp_path):
    comms = context_comms(tmp_path)
    worktree = str(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        "cli", frozenset({"team"}), worktree, execution=ExternalThreadExecution,
    ))
    viewer = comms.messaging.user_identity(worktree).name
    comms.messaging.send("cli", "Alice", "Agent direct message")
    comms.messaging.send("cli", viewer, "Human direct message")
    comms.messaging.send("cli", "#team", "Channel message")
    return comms


def test_named_targets_share_count_changes_and_human_read_contracts(named_target_journey):
    comms = named_target_journey
    worktree = comms.registry.require("cli").worktree
    direct: NamedTarget = CommandTarget.named("cli")
    channel: NamedTarget = CommandTarget.named("#team")
    for target in (direct, channel):
        assert comms.bus.pending_count("Alice", target) == 1

    snapshot = comms.registry.snapshot()
    catalog = comms.channels.catalog.read()
    activity = comms.bus.bus.activity_clocks()
    assert direct.changed_threads(snapshot.threads, snapshot, catalog, activity) == ("cli",)
    assert frozenset(channel.changed_threads(snapshot.threads, snapshot, catalog, activity)) == {
        "sender", "Alice", "Bob", "cli",
    }

    before = comms.views.viewer_snapshot(worktree)
    assert before.unread["cli"].count == 1
    assert before.channel_unread["#team"].count == 1
    comms.views.mark_user_view_read(direct.name, worktree=worktree)
    after_direct = comms.views.viewer_snapshot(worktree)
    assert after_direct.unread["cli"].count == 0
    assert after_direct.channel_unread["#team"].count == 1
    comms.views.mark_user_view_read(channel.name, worktree=worktree)
    after_channel = comms.views.viewer_snapshot(worktree)
    assert after_channel.unread["cli"].count == 0
    assert after_channel.channel_unread["#team"].count == 0
    for target in (direct, channel):
        assert comms.bus.pending_count("Alice", target) == 1
