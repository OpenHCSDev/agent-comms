"""Read evidence follows displayed messages across projections and restarts."""

import random
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination_errors import DMPaintedBoundChanged, StaleRevision
from agent_comms.comms import Comms, wire
from agent_comms.message_page import MessagePageRequest, PageBounds
from agent_comms.page_traversal import OlderTraversal
from agent_comms.threads import Thread
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.read_basis import NamedViewer, ReadCursor
from agent_comms.presentation import ReadThrough


def prepared(root):
    comms = wire(root)
    for name, tags in (("alice", {"team"}), ("bob", set())):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(tags), str(root)))
    return comms


def test_read_request_requires_both_fields_and_page_reader_owns_evidence(tmp_path):
    comms = prepared(tmp_path)
    message = comms.messaging.send("alice", "#team", "shown").message
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    anonymous = comms.views.presentation.channel_page("#team")
    assert anonymous.display_scope.displayed is None
    assert not anonymous.selection.has_evidence
    named = comms.views.presentation.channel_page("#team", viewer=NamedViewer(viewer))
    assert named.display_scope.require_displayed().viewer == viewer
    assert named.selection.has_evidence
    assert message.seq not in comms.bus.reads.cursor(viewer, comms.registry.snapshot())
    with pytest.raises(TypeError, match="expected_scope"):
        ReadThrough(message.seq)
    with pytest.raises(TypeError, match="through"):
        ReadThrough(expected_scope=named.display_scope)
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), mark=ReadThrough(message.seq, named.display_scope),
    )
    assert message.seq in comms.bus.reads.cursor(viewer, comms.registry.snapshot())


def test_typed_read_keys_round_trip_through_the_store_codec():
    from agent_comms.field_codec import FieldCodec
    from agent_comms.read_basis import Conversation
    from agent_comms.read_ledger import ReadDocument

    owner = RecordedThreadIncarnation("viewer", 1.0)
    conversation = Conversation(participants=(owner,))
    document = ReadDocument(messages={owner: {conversation: ReadCursor(2)}})
    encoded = FieldCodec.encode(document)
    assert encoded["messages"][0][0] == {"name": "viewer", "created_at": 1.0}
    assert FieldCodec.decode(ReadDocument, encoded) == document


def test_sidebar_presents_pending_read_reset_notice_once(tmp_path):
    comms = Comms(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    notice = "Read positions were reset; reopen conversations to review them."
    comms.bus.reads.replace(replace(comms.bus.reads.read(), notice=notice))
    assert comms.bus.reads.take_notice(None) is None
    assert comms.bus.reads.read().notice == notice

    first = comms.views.presentation.roster(viewer)
    assert first.read_marker_notice == notice
    assert comms.bus.reads.read().notice is None

    second = Comms(tmp_path).views.presentation.roster(viewer)
    assert second.read_marker_notice is None


def test_bounded_page_marks_only_shown_messages(tmp_path):
    comms = prepared(tmp_path)
    for i in range(5):
        comms.messaging.send("alice", "#team", f"message {i}").message.message_id
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name), bounds=PageBounds(limit=2))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), mark=ReadThrough(page.newest_seq, page.display_scope)
    )
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 3


def test_displayed_message_is_read_in_every_projection(tmp_path):
    comms = prepared(tmp_path)
    comms.messaging.send("alice", "#team", "visible in both views").message.message_id
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), mark=ReadThrough(page.newest_seq, page.display_scope)
    )
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#any"] == 0


def test_any_mode_expansion_cannot_read_unshown_message_in_same_conversation(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('carol', frozenset(), str(tmp_path)))
    hidden = comms.messaging.send("bob", "carol", "no member mentioned").message
    shown = comms.messaging.send("bob", "carol", "@alice shown by mention").message
    comms.channels.set_channel_any_mode("#team", True)
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    assert [message.seq for message in page.messages] == [shown.seq]
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), mark=ReadThrough(shown.seq, page.display_scope)
    )
    comms.channels.update_tags("bob", add=frozenset({"team"}))
    reopened = wire(tmp_path)
    viewer = reopened.messaging.user_identity(str(tmp_path)).name
    seen = reopened.bus.reads.cursor(viewer, reopened.registry.snapshot())
    assert hidden.seq not in seen and shown.seq in seen
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] >= 1


def test_turn_lease_and_unrelated_registry_changes_do_not_invalidate_dm(tmp_path):
    import os

    comms = prepared(tmp_path)
    peer = comms.registry.require("alice")
    comms.registry.register(replace(peer, process_identity=ProcessIdentity.capture(os.getpid())))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", viewer, "painted").message.message_id
    page = comms.views.dm_display_page("alice", worktree=str(tmp_path))
    assert page.display_basis.peer_created_at == page.display_basis.peer_created_at
    comms.registry.lease_local_turn("alice", "ordinary-turn")
    fresh = comms.views.dm_display_page("alice", worktree=str(tmp_path))
    assert fresh.display_basis.peer_created_at == page.display_basis.peer_created_at
    comms.registry.register(comms.registry.new_thread('unrelated', frozenset(), str(tmp_path)))
    comms.views.mark_dm_view_read(
        "alice",
        worktree=str(tmp_path),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    assert wire(tmp_path).bus.pending_count(viewer, "alice") == 0


def test_agent_sent_dm_includes_independent_human_read(tmp_path):
    from agent_comms.thread_identity import UserThreadRole

    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('reader', frozenset(), str(tmp_path), role=UserThreadRole))
    message = comms.messaging.send("alice", "reader", "painted agent DM").message
    snapshot = comms.registry.snapshot()
    displayed = comms.bus.reads.capture(
        "reader", (message,), snapshot, comms.bus.log.path
    )
    comms.bus.reads.mark_displayed("reader", displayed)
    sender_view = comms.views.thread_presentations(("alice",)).require("alice")
    assert sender_view is not None
    outcome = next(
        item for item in sender_view.sent_notifications
        if item.message.reference == message.reference
    )
    assert outcome.displayed_to == (snapshot.require("reader").incarnation,)
    assert outcome.changed

    from types import SimpleNamespace
    from agent_comms.selected_turn import SelectedPrompt

    sent_outcome = outcome.sent_outcome()
    assert sent_outcome is not None
    selected = SelectedPrompt(SimpleNamespace(
        comms=comms,
        owner=SimpleNamespace(thread=comms.registry.require("alice")),
        sent_outcomes=(sent_outcome,),
    ))
    assert [item.message.reference for item in selected.sent_outcomes()] == [message.reference]


def test_dm_ack_survives_process_exit_and_rebind_does_not_inherit_reads(tmp_path):
    import subprocess
    import sys

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    old = comms.messaging.send("alice", viewer, "painted before exit").message
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from agent_comms.comms import wire\n"
            "comms = wire(sys.argv[1])\n"
            "page = comms.views.dm_display_page('alice', worktree=sys.argv[1])\n"
            "comms.views.mark_dm_view_read('alice', worktree=sys.argv[1], "
            "through=page.newest_seq, expected_display_basis=page.display_basis)\n"
            "os._exit(9)\n",
            str(tmp_path),
        ],
        timeout=15,
    )
    assert child.returncode == 9
    comms = wire(tmp_path)
    assert comms.bus.pending_count(viewer, "alice") == 0
    comms.registry.unregister("alice")
    comms.registry.remove("alice")
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    fresh = comms.messaging.send("alice", viewer, "new incarnation unseen").message
    assert old.seq not in comms.bus.reads.cursor(viewer, comms.registry.snapshot())
    assert comms.bus.pending_count(viewer, "alice") == 1
    assert [row.seq for row in comms.bus.inbox(viewer, "alice")] == [fresh.seq]


@pytest.mark.parametrize("seed", [7, 31, 99])
def test_shown_only_property_across_pages_modes_and_reopen(tmp_path, seed):
    rng = random.Random(seed)
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    shown = set()
    for i in range(12):
        comms.messaging.send("alice", rng.choice(["bob", "#team"]), f"row {i}").message.message_id
    for _ in range(18):
        comms.channels.set_channel_any_mode("#team", rng.choice([True, False]))
        page = comms.views.channel_display_page(
            rng.choice(["#team", "#any"]),
            viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name),
            bounds=PageBounds(
                limit=rng.randrange(1, 8), traversal=OlderTraversal(rng.choice([None, 6, 10]))
            ),
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
                mark=ReadThrough(through, scope),
            )
        comms = wire(tmp_path)
        seen = comms.bus.reads.cursor(viewer, comms.registry.snapshot())
        assert seen == ReadCursor().add(tuple(shown))


def test_new_view_predicate_needs_no_ledger_schema_or_dispatch_change(tmp_path):
    from agent_comms.read_basis import ChannelDisplayScope

    class AlternateMessages(ChannelDisplayScope):
        def includes(self, message):
            return message.seq % 2 == 0

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for i in range(6):
        comms.messaging.send("alice", "#team", str(i)).message.message_id
    scope = AlternateMessages("alternating", None)
    page = MessagePageRequest(scope).read(comms.bus.log)
    basis = comms.bus.reads.capture(
        viewer, page.messages, comms.registry.snapshot(), comms.bus.log.path
    )
    comms.bus.reads.mark_displayed(viewer, basis)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 3
    assert comms.bus.reads.cursor(viewer, comms.registry.snapshot()) == ReadCursor(exceptions=(2, 4, 6))


def test_replaced_bus_keeps_no_sequence_read_facts(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", "#team", "painted").message
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), mark=ReadThrough(page.newest_seq, page.display_scope)
    )
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(comms.bus.log.path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(comms.bus.log.path)
    comms.views.viewer_snapshot(str(tmp_path))
    assert not comms.bus.reads.cursor(viewer, comms.registry.snapshot())


def test_recorded_bus_identity_is_one_member(tmp_path):
    from agent_comms.coordination_errors import DMScopeChanged
    from agent_comms.field_codec import FieldCodec
    from agent_comms.private_path import FileIdentity, ObservedPath
    from agent_comms.read_ledger import ReadDocument
    from agent_comms.recorded_bus import NoRecordedBus, RecordedBus

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("alice", viewer, "read before restore").message
    ledger = comms.bus.reads
    assert ledger.read().bus_identity == NoRecordedBus()
    assert not ledger.read().bus_identity.matches(ObservedPath.at(comms.bus.log.path))
    assert comms.views.viewer_snapshot(str(tmp_path)).unread["alice"] == 1

    page = comms.views.dm_display_page("alice", worktree=str(tmp_path))
    comms.views.mark_dm_view_read("alice", worktree=str(tmp_path), through=message.seq,
        expected_display_basis=page.display_basis)
    recorded = RecordedBus(FileIdentity.from_stat(comms.bus.log.path.stat()))
    assert ledger.read().bus_identity == recorded == page.display_basis.bus_identity
    assert recorded.matches(ObservedPath.at(comms.bus.log.path))
    old = FieldCodec.encode(ledger.read())
    assert old["bus_identity"] == [recorded.identity.device, recorded.identity.inode]
    assert FieldCodec.decode(ReadDocument, old) == ledger.read()
    unrecorded = FieldCodec.decode(ReadDocument, {**old, "bus_identity": None})
    assert unrecorded.bus_identity == NoRecordedBus()
    assert not unrecorded.cursor(viewer, comms.registry.snapshot(), ObservedPath.at(comms.bus.log.path))
    assert comms.views.viewer_snapshot(str(tmp_path)).unread.get("alice", 0) == 0

    replacement = tmp_path / "restored-bus.jsonl"
    replacement.write_bytes(comms.bus.log.path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(comms.bus.log.path)
    reopened = wire(tmp_path)
    assert not recorded.matches(ObservedPath.at(reopened.bus.log.path))
    assert message.seq not in reopened.bus.reads.cursor(viewer, reopened.registry.snapshot())
    assert reopened.views.viewer_snapshot(str(tmp_path)).unread["alice"] == 1
    with pytest.raises(DMScopeChanged, match="bus was replaced"):
        reopened.views.mark_dm_view_read("alice", worktree=str(tmp_path), through=message.seq,
            expected_display_basis=page.display_basis)
    fresh = reopened.views.dm_display_page("alice", worktree=str(tmp_path))
    reopened.views.mark_dm_view_read("alice", worktree=str(tmp_path), through=message.seq,
        expected_display_basis=fresh.display_basis)
    assert reopened.views.viewer_snapshot(str(tmp_path)).unread.get("alice", 0) == 0


def test_partial_paints_share_one_basis_across_read_progress(tmp_path):
    comms = prepared(tmp_path)
    messages = [comms.messaging.send("alice", "#team", f"row {i}").message for i in range(5)]
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name), bounds=PageBounds(limit=3))
    scope = page.display_scope
    assert scope is not None and scope.displayed is not None
    # Requested sequences outside the original page cannot become read facts.
    first = scope.displayed.select({messages[0].seq, messages[2].seq, 999999})
    assert [n for item in first.conversations for n in item.sequences] == [messages[2].seq]
    for selected in (first, scope.displayed.select({messages[4].seq})):
        comms.views.mark_channel_view_read(
            "#team",
            worktree=str(tmp_path),
            mark=ReadThrough(page.newest_seq, replace(scope, displayed=selected)),
        )
        # Unrelated registry changes and previous ACKs do not invalidate the
        # remaining captured proof; participant identity still does.
        comms.registry.register(comms.registry.new_thread('unrelated', frozenset(), str(tmp_path)))
    reopened = wire(tmp_path)
    viewer = reopened.messaging.user_identity(str(tmp_path)).name
    assert reopened.bus.reads.cursor(viewer, reopened.registry.snapshot()) == ReadCursor(exceptions=(messages[2].seq, messages[4].seq))
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 3


@pytest.mark.parametrize("seed", [7, 31, 99])
def test_read_property_includes_dm_rebind_stale_paints_and_abrupt_reopen(tmp_path, seed):
    """A recorded read must have an actual paint in the same participant incarnation."""
    import os
    import subprocess
    import sys

    rng = random.Random(seed)
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for target in (viewer, "#team"):
        comms.messaging.send("alice", target, "initial unread").message.message_id
    painted, acknowledged = set(), set()
    pending = []

    def record(basis):
        return {
            (sequence, tuple((p.name, p.created_at) for p in item.conversation.participants))
            for item in basis.conversations
            for sequence in item.sequences
        }

    def current_sequences(facts):
        threads = comms.registry.snapshot().threads
        return {
            sequence
            for sequence, participants in facts
            if all(
                name in threads and threads[name].created_at == creation
                for name, creation in participants
            )
        }

    # Every run includes each operation, in shuffled order, rather than relying
    # on a random draw to happen to exercise a restart or identity replacement.
    operations = ["send", "display", "mark", "mode", "rebind", "crash"] * 6
    rng.shuffle(operations)
    for index, operation in enumerate(operations):
        if operation == "send":
            comms.messaging.send("alice", rng.choice([viewer, "#team", "bob"]), f"row {index}").message.message_id
        elif operation == "mode":
            comms.channels.set_channel_any_mode("#team", rng.choice([True, False]))
        elif operation == "display":
            if rng.choice([True, False]):
                page = comms.views.dm_display_page("alice", worktree=str(tmp_path), bounds=PageBounds(limit=3))
                basis = page.display_basis.displayed
                kind, target = "dm", "alice"
                proof = page.display_basis
            else:
                target = rng.choice(["#team", "#any"])
                page = comms.views.channel_display_page(target, viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name), bounds=PageBounds(limit=3))
                basis = page.display_scope.displayed
                kind, proof = "channel", page.display_scope
            selected = {m.seq for m in page.messages if rng.choice([True, False])}
            basis = basis.select(selected)
            painted.update(record(basis))
            if page.newest_seq is not None:
                pending.append((kind, target, page.newest_seq, replace(proof, displayed=basis)))
        elif operation == "mark" and pending:
            kind, target, through, proof = rng.choice(pending)
            try:
                if kind == "dm":
                    comms.views.mark_dm_view_read(
                        target,
                        worktree=str(tmp_path),
                        through=through,
                        expected_display_basis=proof,
                    )
                else:
                    comms.views.mark_channel_view_read(
                        target, worktree=str(tmp_path), mark=ReadThrough(through, proof)
                    )
            except StaleRevision as error:
                # A stale incarnation is a refused paint, not permission to
                # acknowledge a new peer using an old page.
                stale_participant = any(
                    not p.current(comms.registry.snapshot())
                    for item in proof.displayed.conversations
                    for p in item.conversation.participants
                )
                # A mark past what its page painted is refused for either page kind.
                changed_projection = isinstance(error, DMPaintedBoundChanged)
                assert stale_participant or changed_projection, (kind, str(error))
            else:
                acknowledged.update(record(proof.displayed.through(through)))
        elif operation == "rebind":
            comms.registry.unregister("alice")
            comms.registry.remove("alice")
            comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
        elif operation == "crash":
            # The child loads the actual durable store, then exits without
            # Python cleanup. No synthetic read document or mocked reopen.
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; from agent_comms.comms import wire; "
                    "wire(sys.argv[1]).bus.reads.read(); os._exit(9)",
                    str(tmp_path),
                ],
                env=dict(os.environ),
                capture_output=True,
                timeout=15,
            )
            assert child.returncode == 9, child.stderr.decode()
            comms = wire(tmp_path)
        seen = comms.bus.reads.cursor(viewer, comms.registry.snapshot())
        assert ReadCursor.union((seen, ReadCursor().add(tuple(current_sequences(painted))))) == ReadCursor().add(tuple(current_sequences(painted))), (seed, index, operation)
        assert seen == ReadCursor().add(tuple(current_sequences(acknowledged))), (seed, index, operation)
