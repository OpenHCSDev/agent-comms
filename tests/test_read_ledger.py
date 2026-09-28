"""Read evidence follows displayed messages across projections and restarts."""

import random
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread


def prepared(root):
    comms = wire(root)
    for name, tags in (("alice", {"team"}), ("bob", set())):
        comms.threads.register(Thread(name, frozenset(tags), str(root)))
    return comms


def test_bounded_page_marks_only_shown_messages(tmp_path):
    comms = prepared(tmp_path)
    for i in range(5):
        comms.messaging.send("alice", "#team", f"message {i}")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path), limit=2)
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq, expected_scope=page.display_scope
    )
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 3


def test_displayed_message_is_read_in_every_projection(tmp_path):
    comms = prepared(tmp_path)
    comms.messaging.send("alice", "#team", "visible in both views")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq, expected_scope=page.display_scope
    )
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0


def test_any_mode_expansion_cannot_read_unshown_message_in_same_conversation(tmp_path):
    comms = prepared(tmp_path)
    comms.threads.register(Thread("carol", frozenset(), str(tmp_path)))
    hidden = comms.messaging.send_message("bob", "carol", "no member mentioned")
    shown = comms.messaging.send_message("bob", "carol", "@alice shown by mention")
    comms.channels.set_channel_any_mode("#team", True)
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    assert [message.seq for message in page.messages] == [shown.seq]
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=shown.seq, expected_scope=page.display_scope
    )
    comms.channels.update_tags("bob", add=frozenset({"team"}))
    reopened = wire(tmp_path)
    viewer = reopened.messaging.user_identity(str(tmp_path)).name
    seen = reopened.bus.reads.seen_sequences(viewer, reopened.registry.snapshot())
    assert hidden.seq not in seen and shown.seq in seen
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] >= 1


def test_turn_lease_and_unrelated_registry_changes_do_not_invalidate_dm(tmp_path):
    import os

    comms = prepared(tmp_path)
    peer = comms.registry.require("alice")
    comms.registry.register(replace(peer, pid=os.getpid()))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", viewer, "painted")
    page = comms.views.dm_display_page("alice", worktree=str(tmp_path))
    assert page.display_basis.peer_created_at == page.display_basis.peer_created_at
    comms.registry.lease_local_turn("alice", "ordinary-turn")
    fresh = comms.views.dm_display_page("alice", worktree=str(tmp_path))
    assert fresh.display_basis.peer_created_at == page.display_basis.peer_created_at
    comms.registry.register(Thread("unrelated", frozenset(), str(tmp_path)))
    comms.views.mark_dm_view_read(
        "alice",
        worktree=str(tmp_path),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    assert wire(tmp_path).bus.pending_count(viewer, "alice") == 0


def test_dm_ack_survives_process_exit_and_rebind_does_not_inherit_reads(tmp_path):
    import subprocess
    import sys

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    old = comms.messaging.send_message("alice", viewer, "painted before exit")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "\nimport os, sys\nfrom agent_comms.comms import wire\ncomms = wire(sys.argv[1])\npage = comms.views.dm_display_page('alice', worktree=sys.argv[1])\ncomms.views.mark_dm_view_read('alice', worktree=sys.argv[1], through=page.newest_seq, expected_display_basis=page.display_basis)\nos._exit(9)\n",
            str(tmp_path),
        ],
        timeout=15,
    )
    assert child.returncode == 9
    comms = wire(tmp_path)
    assert comms.bus.pending_count(viewer, "alice") == 0
    prior = comms.registry.require("alice")
    comms.registry.unregister("alice")
    comms.registry.remove("alice")
    comms.threads.register(replace(prior, created_at=prior.created_at + 1))
    assert old.seq not in comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
    assert comms.bus.pending_count(viewer, "alice") == 1


@pytest.mark.parametrize("seed", [7, 31, 99])
def test_shown_only_property_across_pages_modes_and_reopen(tmp_path, seed):
    rng = random.Random(seed)
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    shown = set()
    for i in range(12):
        comms.messaging.send("alice", rng.choice(["bob", "#team"]), f"row {i}")
    for _ in range(18):
        comms.channels.set_channel_any_mode("#team", rng.choice([True, False]))
        page = comms.views.channel_display_page(
            rng.choice(["#team", "#any"]),
            worktree=str(tmp_path),
            limit=rng.randrange(1, 8),
            before=rng.choice([None, 6, 10]),
        )
        if page.messages:
            through = rng.choice(page.messages).seq
            painted = {message.seq for message in page.messages if rng.choice([True, False])}
            shown.update(sequence for sequence in painted if sequence <= through)
            scope = page.display_scope
            scope = replace(scope, displayed=scope.displayed.select(painted))
            comms.views.mark_channel_view_read(
                scope.channel,
                worktree=str(tmp_path),
                through=through,
                expected_scope=scope,
            )
        comms = wire(tmp_path)
        seen = comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())
        assert seen == shown


def test_new_view_predicate_needs_no_ledger_schema_or_dispatch_change(tmp_path):
    from agent_comms.read_basis import ChannelDisplayScope

    class AlternateMessages(ChannelDisplayScope):
        def includes(self, message):
            return message.seq % 2 == 0

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for i in range(6):
        comms.messaging.send("alice", "#team", str(i))
    scope = AlternateMessages("alternating", None)
    page = comms.bus.channel_display_page(scope)
    basis = comms.bus.reads.capture(viewer, page.messages, comms.registry.snapshot(), comms.bus.log.path)
    comms.bus.reads.mark_displayed(viewer, basis)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 3
    assert comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot()) == {2, 4, 6}


def test_replaced_bus_cannot_inherit_sequence_read_facts(tmp_path):
    import json

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send_message("alice", "#team", "painted")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq, expected_scope=page.display_scope
    )
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(json.dumps(replace(message, body="never painted").to_wire()) + "\n")
    replacement.replace(comms.bus.log.path)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 1
    assert not comms.bus.reads.seen_sequences(viewer, comms.registry.snapshot())


def test_partial_paints_share_one_basis_across_read_progress(tmp_path):
    comms = prepared(tmp_path)
    messages = [comms.messaging.send_message("alice", "#team", f"row {i}") for i in range(5)]
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path), limit=3)
    scope = page.display_scope
    assert scope is not None and scope.displayed is not None
    # Requested sequences outside the original page cannot become read facts.
    first = scope.displayed.select({messages[0].seq, messages[2].seq, 999999})
    assert [n for item in first.conversations for n in item.sequences] == [messages[2].seq]
    for selected in (first, scope.displayed.select({messages[4].seq})):
        comms.views.mark_channel_view_read(
            "#team",
            worktree=str(tmp_path),
            through=page.newest_seq,
            expected_scope=replace(scope, displayed=selected),
        )
        # Unrelated registry changes and previous ACKs do not invalidate the
        # remaining captured proof; participant identity still does.
        comms.registry.register(Thread("unrelated", frozenset(), str(tmp_path)))
    reopened = wire(tmp_path)
    viewer = reopened.messaging.user_identity(str(tmp_path)).name
    assert reopened.bus.reads.seen_sequences(viewer, reopened.registry.snapshot()) == {
        messages[2].seq,
        messages[4].seq,
    }
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 3
