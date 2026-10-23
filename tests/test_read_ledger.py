"""Read evidence follows displayed messages across projections and restarts."""


import random
from dataclasses import replace

import pytest

from agent_comms.bus import Bus
from agent_comms.channel_targets import CommandTarget, ThreadTarget
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_errors import DMPaintedBoundChanged, StaleRevision
from agent_comms.field_codec import FieldCodec
from agent_comms.message_page import MessagePageRequest, PageBounds
from agent_comms.page_traversal import PageCursor
from agent_comms.presentation import ReadThrough
from agent_comms.read_basis import (
    ChannelConversation,
    ConversationCursor,
    DMConversation,
    NamedViewer,
    NoDisplaySelection,
    ReadCursor,
)
from agent_comms.read_ledger import (
    NoReadNotice,
    PendingReadNotice,
    ReadDocument,
    ReadLedger,
    ReadNotice,
)
from agent_comms.thread_identity import RecordedThreadIncarnation
from bus_record_fixture import prepare_native_comms


def prepared(root):
    comms = prepare_native_comms(wire(root))
    for name, tags in (("alice", {"team"}), ("bob", set())):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(tags), str(root)))
    return comms


def read_message(comms, viewer, message):
    snapshot = comms.registry.snapshot()
    key = ReadLedger.conversation(message, snapshot)
    return message.seq in comms.bus.reads.cursor(viewer, snapshot).for_conversation(key)


def test_delivery_watermark_keeps_other_peer_and_newer_messages_unread(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    first = comms.messaging.send('alice', viewer, 'first').message
    later = comms.messaging.send('alice', viewer, 'later').message
    other = comms.messaging.send('bob', viewer, 'other peer').message
    comms.bus.mark_delivered_through(viewer, first.seq, target=CommandTarget.named('alice'))
    assert read_message(comms, viewer, first)
    assert not read_message(comms, viewer, later)
    assert not read_message(comms, viewer, other)
    comms.bus.mark_delivered_through(viewer, later.seq, target=CommandTarget.named('alice'))
    assert read_message(comms, viewer, later)
    assert not read_message(comms, viewer, other)


def test_mark_all_uses_conversation_prefixes_without_sparse_message_lists(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    messages = [comms.messaging.send('alice', viewer, str(i)).message for i in range(3)]
    comms.views.mark_user_view_read('#any', worktree=str(tmp_path))
    assert all(read_message(comms, viewer, message) for message in messages)
    cursor = comms.bus.reads.cursor(viewer, comms.registry.snapshot())
    assert all(not position.exceptions for position in cursor.conversations.values())


def test_member_activity_mark_all_does_not_read_hidden_conversation_rows(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('carol', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    hidden = comms.messaging.send('bob', 'carol', 'outside the selected activity').message
    shown = comms.messaging.send('bob', 'carol', '@alice selected activity').message
    comms.channels.set_channel_any_mode('#team', True)
    comms.views.mark_user_view_read('#team', worktree=str(tmp_path))
    assert read_message(comms, viewer, shown)
    assert not read_message(comms, viewer, hidden)


def test_read_request_requires_both_fields_and_page_reader_owns_evidence(tmp_path):
    comms = prepared(tmp_path)
    message = comms.messaging.send("alice", "#team", "shown").message
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    anonymous = comms.views.presentation.channel_page("#team")
    assert isinstance(anonymous.selection, NoDisplaySelection)
    assert not anonymous.selection.has_evidence
    with pytest.raises(DMPaintedBoundChanged):
        comms.views.mark_page_read(anonymous, worktree=str(tmp_path))
    named = comms.views.presentation.channel_page("#team", viewer=NamedViewer(viewer))
    assert named.selection.scope.require_displayed().viewer == viewer
    assert named.selection.has_evidence
    assert not read_message(comms, viewer, message)
    with pytest.raises(TypeError):
        ReadThrough(message.seq)
    with pytest.raises(TypeError):
        ReadThrough(expected_scope=named.selection.scope)
    comms.views.mark_page_read(named, worktree=str(tmp_path))
    assert read_message(comms, viewer, message)


def test_typed_read_keys_round_trip_through_the_store_codec():
    owner = RecordedThreadIncarnation("viewer", 1.0)
    conversation = DMConversation(participants=(owner,))
    document = ReadDocument(messages={owner: ReadCursor({conversation: ConversationCursor(2)})})
    encoded = FieldCodec.encode(document)
    assert FieldCodec.decode(ReadDocument, encoded) == document


def test_sidebar_presents_pending_read_reset_notice_once(tmp_path):
    comms = wire(tmp_path)
    Bus.install(Bus.path_in(comms.root))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    notice = PendingReadNotice("Read positions were reset; reopen conversations to review them.")
    comms.bus.reads.replace(replace(comms.bus.reads.read(), notice=notice))
    assert NoReadNotice().take(comms.bus.reads) == NoReadNotice()
    assert comms.bus.reads.read().notice == notice

    first = comms.views.presentation.roster(viewer)
    assert first.read_marker_notice == notice
    assert comms.bus.reads.read().notice == NoReadNotice()

    second = wire(tmp_path).views.presentation.roster(viewer)
    assert second.read_marker_notice == NoReadNotice()


def test_empty_notice_never_accesses_store_and_pending_notice_keeps_a_replacement(tmp_path):
    assert NoReadNotice().take(object()) == NoReadNotice()
    assert FieldCodec.decode(ReadNotice, None) == NoReadNotice()
    assert FieldCodec.decode(ReadNotice, "reset") == PendingReadNotice("reset")
    ledger = ReadLedger(tmp_path / ReadLedger.filename)
    newer = PendingReadNotice("newer reset")
    ledger.replace(ReadDocument(notice=newer))
    assert PendingReadNotice("older reset").take(ledger) == NoReadNotice()
    assert ledger.read().notice == newer
    shown = []
    newer.take(ledger).notify_if_new(NoReadNotice(), shown.append)
    ledger.read().notice.notify_if_new(newer, shown.append)
    newer.notify_if_new(newer, shown.append)
    assert shown == [newer.text]


def test_bounded_page_marks_only_shown_messages(tmp_path):
    comms = prepared(tmp_path)
    for i in range(5):
        comms.messaging.send("alice", "#team", f"message {i}").message.message_id
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name), bounds=PageBounds(limit=2))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 3


def test_displayed_message_is_read_in_every_projection(tmp_path):
    comms = prepared(tmp_path)
    comms.messaging.send("alice", "#team", "visible in both views").message.message_id
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#any"].count == 0


def test_any_mode_expansion_cannot_read_unshown_message_in_same_conversation(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(comms.registry.new_thread('carol', frozenset(), str(tmp_path)))
    hidden = comms.messaging.send("bob", "carol", "no member mentioned").message
    shown = comms.messaging.send("bob", "carol", "@alice shown by mention").message
    comms.channels.set_channel_any_mode("#team", True)
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    assert [message.seq for message in page.messages] == [shown.seq]
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    comms.channels.update_tags("bob", add=frozenset({"team"}))
    reopened = wire(tmp_path)
    viewer = reopened.messaging.user_identity(str(tmp_path)).name
    seen = reopened.bus.reads.cursor(viewer, reopened.registry.snapshot())
    key = ReadLedger.conversation(hidden, reopened.registry.snapshot())
    assert hidden.seq not in seen.for_conversation(key) and shown.seq in seen.for_conversation(key)
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count >= 1


def test_turn_lease_and_unrelated_registry_changes_do_not_invalidate_dm(tmp_path):
    import os

    comms = prepared(tmp_path)
    peer = comms.registry.require("alice")
    comms.registry.register(replace(peer, process_identity=ProcessIdentity.capture(os.getpid())))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", viewer, "painted").message.message_id
    page = comms.views.dm_display_page(ThreadTarget("alice"), worktree=str(tmp_path))
    assert page.selection.basis.peer_identity == peer.incarnation
    comms.registry.lease_local_turn("alice", "ordinary-turn")
    fresh = comms.views.dm_display_page(ThreadTarget("alice"), worktree=str(tmp_path))
    assert fresh.selection.basis.peer_identity == page.selection.basis.peer_identity
    comms.registry.register(comms.registry.new_thread('unrelated', frozenset(), str(tmp_path)))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert wire(tmp_path).bus.pending_count(viewer, CommandTarget.named("alice")) == 0


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
            "from agent_comms.channel_targets import ThreadTarget\n"
            "from agent_comms.comms import wire\n"
            "comms = wire(sys.argv[1])\n"
            "page = comms.views.dm_display_page(ThreadTarget('alice'), worktree=sys.argv[1])\n"
            "comms.views.mark_page_read(page, worktree=sys.argv[1])\n"
            "os._exit(9)\n",
            str(tmp_path),
        ],
        timeout=15,
    )
    assert child.returncode == 9
    comms = wire(tmp_path)
    assert comms.bus.pending_count(viewer, CommandTarget.named("alice")) == 0
    comms.registry.unregister("alice")
    comms.registry.remove("alice")
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    fresh = comms.messaging.send("alice", viewer, "new incarnation unseen").message
    assert not read_message(comms, viewer, old)
    assert comms.bus.pending_count(viewer, CommandTarget.named("alice")) == 1
    assert [row.seq for row in comms.bus.inbox(viewer, CommandTarget.named("alice"))] == [fresh.seq]


@pytest.mark.parametrize("seed", [7, 31, 99])
def test_shown_only_property_across_pages_modes_and_reopen(tmp_path, seed):
    rng = random.Random(seed)
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    shown = set()
    messages = [comms.messaging.send("alice", rng.choice(["bob", "#team"]), f"row {i}").message
                for i in range(12)]
    for _ in range(18):
        comms.channels.set_channel_any_mode("#team", rng.choice([True, False]))
        page = comms.views.channel_display_page(
            rng.choice(["#team", "#any"]),
            viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name),
            bounds=PageBounds(
                limit=rng.randrange(1, 8), traversal=FieldCodec.decode(PageCursor, rng.choice([None, 6, 10])).older()
            ),
        )
        if page.messages:
            through = rng.choice(page.messages).seq
            painted = {message.seq for message in page.messages if rng.choice([True, False])}
            shown.update(sequence for sequence in painted if sequence <= through)
            selected = page.select(sequence for sequence in painted if sequence <= through)
            if selected.messages:
                comms.views.mark_page_read(selected, worktree=str(tmp_path))
        comms = wire(tmp_path)
        seen = comms.bus.reads.cursor(viewer, comms.registry.snapshot())
        for message in messages:
            key = ReadLedger.conversation(message, comms.registry.snapshot())
            assert (message.seq in seen.for_conversation(key)) == (message.seq in shown)


def test_new_view_predicate_needs_no_ledger_schema_or_dispatch_change(tmp_path):
    from agent_comms.read_basis import AllChannelDisplayScope

    class AlternateMessages(AllChannelDisplayScope):
        @property
        def index_predicate(self):
            return "seq % 2 = 0", ()

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for i in range(6):
        comms.messaging.send("alice", "#team", str(i))
    scope = AlternateMessages("alternating")
    page = MessagePageRequest(scope).read_opened(comms.bus.bus)
    basis = comms.bus.reads.capture(
        viewer, page.messages, comms.registry.snapshot(), comms.bus.bus,
        document=comms.bus.reads.read())
    comms.bus.reads.mark_displayed(viewer, basis)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 3
    assert comms.bus.reads.cursor(viewer, comms.registry.snapshot()) == ReadCursor({ChannelConversation(target="#team"): ConversationCursor(exceptions=(2, 4, 6))})


def test_single_participant_dm_retains_its_scope_and_sql_identity(tmp_path):
    comms = prepared(tmp_path)
    snapshot = comms.registry.snapshot()
    conversation = DMConversation((snapshot.require("alice").incarnation,))
    scope = conversation.scope(snapshot)
    assert scope.first_names == scope.second_names == snapshot.names_for("alice")
    assert set(conversation.sql_keys(snapshot)) == {("alice", "alice")}


def test_all_read_uses_one_watermark_without_sparse_sequences(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    messages = [
        comms.messaging.send("alice", "#team", "first channel sender").message,
        comms.messaging.send("bob", "#team", "second channel sender").message,
        comms.messaging.send("alice", "bob", "DM forward").message,
        comms.messaging.send("bob", "alice", "DM reverse").message,
    ]
    comms.views.mark_user_view_read("#any", worktree=str(tmp_path))
    snapshot = comms.registry.snapshot()
    seen = comms.bus.reads.cursor(viewer, snapshot)
    from agent_comms.read_basis import AllConversation
    assert tuple(seen.conversations) == (AllConversation(),)
    assert seen.for_conversation(AllConversation()).high_water == messages[-1].seq
    assert all(message.seq in seen.for_conversation(ReadLedger.conversation(message, snapshot))
               for message in messages)
    assert all(not cursor.exceptions for cursor in seen.conversations.values())
    arrived = comms.messaging.send('alice', viewer, 'arrived after mark-all').message
    assert not read_message(comms, viewer, arrived)
    assert comms.bus.pending_count(viewer, CommandTarget.named('alice')) == 1
    comms.views.mark_user_view_read('#any', worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, CommandTarget.named('alice')) == 0


def test_replaced_bus_keeps_no_sequence_read_facts(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", "#team", "painted").message
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    replacement = tmp_path / "replacement.sqlite3"
    comms.bus.bus.backup(replacement)
    replacement.replace(comms.bus.bus.path)
    reopened = wire(tmp_path)
    assert reopened.bus.reads.cursor(viewer, reopened.registry.snapshot()) == ReadCursor()
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 1
    from agent_comms.coordination_errors import DMScopeChanged
    with pytest.raises(DMScopeChanged):
        reopened.views.mark_page_read(page, worktree=str(tmp_path))
    fresh = reopened.views.channel_display_page("#team", viewer=NamedViewer(viewer))
    reopened.views.mark_page_read(fresh, worktree=str(tmp_path))
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 0


def test_recorded_bus_identity_is_one_member(tmp_path):
    from agent_comms.coordination_errors import DMScopeChanged
    from agent_comms.private_path import FileIdentity, ObservedPath
    from agent_comms.read_ledger import ReadDocument
    from agent_comms.recorded_bus import NoRecordedBus, RecordedBus

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("alice", viewer, "read before restore").message
    ledger = comms.bus.reads
    assert ledger.read().bus_identity == NoRecordedBus()
    assert not ledger.read().bus_identity.matches(ObservedPath.at(comms.bus.bus.path))
    assert comms.views.viewer_snapshot(str(tmp_path)).unread["alice"].count == 1

    page = comms.views.dm_display_page(ThreadTarget("alice"), worktree=str(tmp_path))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    recorded = RecordedBus(FileIdentity.from_stat(comms.bus.bus.path.stat()))
    assert ledger.read().bus_identity == recorded == page.selection.basis.bus_identity
    assert recorded.matches(ObservedPath.at(comms.bus.bus.path))
    assert FieldCodec.decode(ReadDocument, FieldCodec.encode(ledger.read())) == ledger.read()
    unrecorded = replace(ledger.read(), bus_identity=NoRecordedBus())
    assert unrecorded.cursor(viewer, comms.registry.snapshot(), ObservedPath.at(comms.bus.bus.path)) == ReadCursor()
    assert comms.views.viewer_snapshot(str(tmp_path)).unread["alice"].count == 0

    replacement = tmp_path / "restored-bus.sqlite3"
    comms.bus.bus.backup(replacement)
    replacement.replace(comms.bus.bus.path)
    reopened = wire(tmp_path)
    assert not recorded.matches(ObservedPath.at(reopened.bus.bus.path))
    snapshot = reopened.registry.snapshot()
    assert message.seq not in reopened.bus.reads.cursor(viewer, snapshot).for_conversation(
        ReadLedger.conversation(message, snapshot))
    assert reopened.views.viewer_snapshot(str(tmp_path)).unread["alice"].count == 1
    with pytest.raises(DMScopeChanged):
        reopened.views.mark_page_read(page, worktree=str(tmp_path))
    fresh = reopened.views.dm_display_page(ThreadTarget("alice"), worktree=str(tmp_path))
    reopened.views.mark_page_read(fresh, worktree=str(tmp_path))
    assert reopened.views.viewer_snapshot(str(tmp_path)).unread["alice"].count == 0


def test_partial_paints_share_one_basis_across_read_progress(tmp_path):
    comms = prepared(tmp_path)
    messages = [comms.messaging.send("alice", "#team", f"row {i}").message for i in range(5)]
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name), bounds=PageBounds(limit=3))
    scope = page.selection.scope
    assert scope is not None and scope.displayed is not None
    # Requested sequences outside the original page cannot become read facts.
    first = scope.displayed.select({messages[0].seq, messages[2].seq, 999999})
    assert [n for item in first.conversations for n in item.sequences] == [messages[2].seq]
    for selected in (first, scope.displayed.select({messages[4].seq})):
        comms.views.mark_page_read(
            replace(page, selection=replace(page.selection, scope=replace(scope, displayed=selected))),
            worktree=str(tmp_path),
        )
        # Unrelated registry changes and previous ACKs do not invalidate the
        # remaining captured proof; participant identity still does.
        comms.registry.register(comms.registry.new_thread('unrelated', frozenset(), str(tmp_path)))
    reopened = wire(tmp_path)
    viewer = reopened.messaging.user_identity(str(tmp_path)).name
    assert reopened.bus.reads.cursor(viewer, reopened.registry.snapshot()) == ReadCursor({ChannelConversation(target="#team"): ConversationCursor(exceptions=(messages[2].seq, messages[4].seq))})
    assert reopened.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 3


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

    def current_cursor(facts):
        threads = comms.registry.snapshot().threads
        conversations = {}
        for sequence, participants in facts:
            if all(name in threads and threads[name].created_at == birth
                   for name, birth in participants):
                key = (DMConversation(participants=tuple(RecordedThreadIncarnation(name, birth)
                                                       for name, birth in participants))
                       if participants else ChannelConversation(target="#team"))
                conversations[key] = conversations.get(key, ConversationCursor()).add((sequence,))
        return ReadCursor(conversations)

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
                page = comms.views.dm_display_page(ThreadTarget("alice"), worktree=str(tmp_path), bounds=PageBounds(limit=3))
                basis = page.selection.basis.displayed
                kind, target = "dm", "alice"
                proof = page.selection.basis
            else:
                target = rng.choice(["#team", "#any"])
                page = comms.views.channel_display_page(target, viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name), bounds=PageBounds(limit=3))
                basis = page.selection.scope.displayed
                kind, proof = "channel", page.selection.scope
            selected = {m.seq for m in page.messages if rng.choice([True, False])}
            basis = basis.select(selected)
            painted.update(record(basis))
            if page.newest_seq is not None:
                pending.append((kind, target, max(selected, default=0), replace(proof, displayed=basis)))
        elif operation == "mark" and pending:
            kind, target, through, proof = rng.choice(pending)
            try:
                if kind == "dm":
                    proof.acknowledge(
                        comms.bus, comms.registry, comms.root, target,
                        worktree=str(tmp_path), through=through,
                    )
                else:
                    comms.views.presentation.mark_channel_read(
                        target, viewer=viewer, mark=ReadThrough(through, proof),
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
        painted_cursor, acknowledged_cursor = current_cursor(painted), current_cursor(acknowledged)
        snapshot = comms.registry.snapshot()
        for message in comms.bus.bus.message_history():
            key = ReadLedger.conversation(message, snapshot)
            read = message.seq in seen.for_conversation(key)
            assert not read or message.seq in painted_cursor.for_conversation(key), (seed, index, operation, message.seq)
            assert read == (message.seq in acknowledged_cursor.for_conversation(key)), (seed, index, operation, message.seq)
