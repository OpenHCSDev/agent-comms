"""Read evidence follows displayed messages across projections and restarts."""

from tests.read_membership import install_read_state, stored_read_state
from agent_comms.read_ledger import AbsentReadSource, NoReadNotice
from tests.read_membership import read_mask
import random
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.threads import Thread
from tests.read_membership import displayed_sequences
from agent_comms.thread_identity import RecordedThreadIncarnation


def prepared(root):
    comms = wire(root)
    for name, tags in (("alice", {"team"}), ("bob", set())):
        comms.registry.declare(Thread(name, frozenset(tags), str(root)))
    return comms


def test_reader_selection_owns_empty_acquisition_not_participant_role(tmp_path):
    from agent_comms.read_ledger import HumanViewers, ReadLedger, SelectedViewers

    ledger = ReadLedger(tmp_path)
    agent = Thread('agent', frozenset(), str(tmp_path), created_at=1.0)
    with HumanViewers.capture((agent,)).reading(ledger, AbsentReadSource()) as document:
        assert document.messages == {}
    assert not ledger.path.exists()
    with pytest.raises(FileNotFoundError):
        with SelectedViewers.capture((agent,)).reading(ledger, AbsentReadSource()):
            pytest.fail('A selected reader cannot recover missing authority')
    assert not ledger.path.exists()


def test_conversation_persisted_pairs_decode_once_without_an_encode_round_trip():
    from unittest.mock import patch

    from agent_comms.field_codec import FieldCodec
    from agent_comms.read_basis import Conversation

    conversation = Conversation(participants=(RecordedThreadIncarnation("alice", 1.0),))
    from agent_comms.read_ledger import ConversationKey, ReadDocument, ReadProgress
    document = ReadDocument({ConversationKey(RecordedThreadIncarnation('viewer', 2.0), conversation): ReadProgress(3)}, AbsentReadSource(), {}, NoReadNotice())
    encoded = FieldCodec.encode(document)
    decoded = FieldCodec.decode(ReadDocument, encoded)
    with patch.object(FieldCodec, 'encode', side_effect=AssertionError('Already decoded')):
        assert decoded == document
        assert next(iter(decoded.messages)).conversation == conversation


@pytest.mark.parametrize("raw", [
    {"target": "#team", "participants": [], "unknown": 1},
    {"target": 2, "participants": []},
    {"participants": [["alice", True]]},
    {"participants": [["alice"]]},
    {"participants": {}},
])
def test_conversation_pair_boundary_retains_strict_codec_validation(raw):
    from agent_comms.read_basis import Conversation

    with pytest.raises((TypeError, ValueError)):
        from agent_comms.field_codec import FieldCodec
        FieldCodec.decode(Conversation, raw)


def test_exact_dm_selection_survives_aliases_and_refuses_unknown_participants(tmp_path, monkeypatch):
    from agent_comms.read_ledger import AbsentReadSource, ReadMessageRow, ReadSource

    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path))
    messages = tuple(
        comms.messaging.send_message(sender, viewer.name, sender)
        for sender in ('alice', 'bob')
    )
    snapshot = comms.registry.snapshot()
    for message in messages:
        comms.bus.reads.mark_displayed(viewer.name, comms.bus.reads.capture(
            viewer.name, (message,), snapshot, comms.bus.log.path
        ), snapshot=snapshot)
    assert len(stored_read_state(comms.bus.reads).messages) == 2
    comms.registry.rename('alice', 'renamed-alice')
    snapshot = comms.registry.snapshot()
    with comms.bus.log.certified_read() as source:
        delivery = source.delivery(messages[0].seq)
        identity = ReadSource.from_revision(source.witness.revision)
    decoded = []
    decode = ReadMessageRow._decode_row.__func__

    def count(cls, row, positions):
        value = decode(cls, row, positions)
        decoded.append(value.key)
        return value

    monkeypatch.setattr(ReadMessageRow, '_decode_row', classmethod(count))
    prepared_reads = comms.bus.reads.capture_observations(snapshot, (delivery,))
    assert len(decoded) == 1  # The unrelated target='' DM never crosses decoding.
    mask = prepared_reads.for_source(identity)[viewer.name]
    assert mask.contains(messages[0]) and not mask.contains(messages[1])
    assert prepared_reads.for_source(AbsentReadSource()) == {}

    def unknown(*args, **kwargs):
        raise KeyError('unknown captured participant')

    monkeypatch.setattr(type(snapshot), 'participant', unknown)
    with pytest.raises(KeyError, match='unknown captured participant'):
        comms.bus.reads.capture_observations(snapshot, (delivery,))


def test_sidebar_presents_pending_read_reset_notice_once(tmp_path):
    comms = Comms(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    from unittest.mock import patch
    from agent_comms.read_ledger import ReadMarkerNotice

    notice = ReadMarkerNotice("Read positions were reset; reopen conversations to review them.")
    install_read_state(comms.bus.reads, replace(stored_read_state(comms.bus.reads), notice=notice))
    with patch.object(type(comms.bus.reads), 'take_notice', side_effect=AssertionError('No notice was shown')):
        assert NoReadNotice().take(comms.bus.reads) == NoReadNotice()
    assert stored_read_state(comms.bus.reads).notice == notice

    first = comms.views.presentation.roster(viewer)
    assert first.read_marker_notice == notice
    assert stored_read_state(comms.bus.reads).notice == NoReadNotice()

    second = Comms(tmp_path).views.presentation.roster(viewer)
    assert second.read_marker_notice == NoReadNotice()


def test_bounded_page_marks_only_shown_messages(tmp_path):
    comms = prepared(tmp_path)
    for i in range(5):
        comms.messaging.send("alice", "#team", f"message {i}")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path), limit=2)
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq, expected_scope=page.display_scope
    )
    assert wire(tmp_path).views.viewer_snapshot(wire(tmp_path).messaging.user_identity(str(tmp_path))).channel_unread["#team"] == 3


def test_displayed_message_is_read_in_every_projection(tmp_path):
    comms = prepared(tmp_path)
    comms.messaging.send("alice", "#team", "visible in both views")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq, expected_scope=page.display_scope
    )
    assert wire(tmp_path).views.viewer_snapshot(wire(tmp_path).messaging.user_identity(str(tmp_path))).channel_unread["#any"] == 0


def test_any_mode_expansion_cannot_read_unshown_message_in_same_conversation(tmp_path):
    comms = prepared(tmp_path)
    comms.registry.declare(Thread("carol", frozenset(), str(tmp_path)))
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
    seen = displayed_sequences(reopened, viewer)
    assert hidden.seq not in seen and shown.seq in seen
    assert reopened.views.viewer_snapshot(reopened.messaging.user_identity(str(tmp_path))).channel_unread["#team"] >= 1


def test_turn_lease_and_unrelated_registry_changes_do_not_invalidate_dm(tmp_path):
    import os

    comms = prepared(tmp_path)
    peer = comms.registry.require("alice")
    comms.registry.register(replace(peer, process_identity=ProcessIdentity.capture(os.getpid())))
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


def test_agent_sent_dm_includes_independent_human_read(tmp_path):
    from agent_comms.thread_identity import ThreadRole

    comms = prepared(tmp_path)
    comms.registry.declare(Thread("reader", frozenset(), str(tmp_path), role=ThreadRole.USER))
    message = comms.messaging.send_message("alice", "reader", "painted agent DM")
    snapshot = comms.registry.snapshot()
    displayed = comms.bus.reads.capture(
        "reader", (message,), snapshot, comms.bus.log.path
    )
    comms.bus.reads.mark_displayed("reader", displayed, snapshot=snapshot)
    sender_view = comms.views.thread_presentation("alice")
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
    old = comms.messaging.send_message("alice", viewer, "painted before exit")
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
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    fresh = comms.messaging.send_message("alice", viewer, "new incarnation unseen")
    assert not read_mask(comms, viewer, comms.registry.snapshot()).contains(old)
    assert comms.bus.pending_count(viewer, "alice") == 1
    assert [row.seq for row in comms.bus.inbox(viewer, "alice")] == [fresh.seq]


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
        seen = displayed_sequences(comms, viewer)
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
    page = comms.bus.display_page(scope)
    basis = comms.bus.reads.capture(
        viewer, page.messages, comms.registry.snapshot(), comms.bus.log.path
    )
    comms.bus.reads.mark_displayed(viewer, basis, snapshot=comms.registry.snapshot())
    assert comms.views.viewer_snapshot(comms.messaging.user_identity(str(tmp_path))).channel_unread["#team"] == 3
    assert displayed_sequences(comms, viewer) == {2, 4, 6}


def test_replaced_bus_cannot_inherit_sequence_read_facts(tmp_path):
    comms = prepared(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send_message("alice", "#team", "painted")
    page = comms.views.channel_display_page("#team", worktree=str(tmp_path))
    comms.views.mark_channel_view_read(
        "#team", worktree=str(tmp_path), through=page.newest_seq, expected_scope=page.display_scope
    )
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(comms.bus.log.path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(comms.bus.log.path)
    from agent_comms.errors import RelationViolationError

    with pytest.raises(RelationViolationError, match="checkpoint root/inode/size changed"):
        comms.views.viewer_snapshot(comms.messaging.user_identity(str(tmp_path)))
    from agent_comms.private_path import FileRevision
    from agent_comms.read_ledger import ReadSource
    with comms.bus.log.path.open('rb') as source:
        identity = ReadSource.from_revision(FileRevision.from_file(source))
        assert not comms.bus.reads.membership(viewer, comms.registry.snapshot(), identity).conversations


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
    assert displayed_sequences(reopened, viewer) == {
        messages[2].seq,
        messages[4].seq,
    }
    assert reopened.views.viewer_snapshot(reopened.messaging.user_identity(str(tmp_path))).channel_unread["#team"] == 3


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
        comms.messaging.send("alice", target, "initial unread")
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
            comms.messaging.send("alice", rng.choice([viewer, "#team", "bob"]), f"row {index}")
        elif operation == "mode":
            comms.channels.set_channel_any_mode("#team", rng.choice([True, False]))
        elif operation == "display":
            if rng.choice([True, False]):
                page = comms.views.dm_display_page("alice", worktree=str(tmp_path), limit=3)
                basis = page.display_basis.displayed
                kind, target = "dm", "alice"
                proof = page.display_basis
            else:
                target = rng.choice(["#team", "#any"])
                page = comms.views.channel_display_page(target, worktree=str(tmp_path), limit=3)
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
                        target, worktree=str(tmp_path), through=through, expected_scope=proof
                    )
            except ValueError as error:
                # A stale incarnation is a refused paint, not permission to
                # acknowledge a new peer using an old page.
                stale_participant = any(
                    not p.current(comms.registry.snapshot())
                    for item in proof.displayed.conversations
                    for p in item.conversation.participants
                )
                changed_projection = kind == "channel" and str(error).startswith(
                    "Channel display changed"
                )
                assert stale_participant or changed_projection, str(error)
            else:
                acknowledged.update(record(proof.displayed.through(through)))
        elif operation == "rebind":
            comms.registry.unregister("alice")
            comms.registry.remove("alice")
            comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
        elif operation == "crash":
            # The child loads the actual durable store, then exits without
            # Python cleanup. No synthetic read document or mocked reopen.
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; from agent_comms.comms import wire; "
                    "from agent_comms.read_ledger import OneViewer; "
                    "comms=wire(sys.argv[1]); viewer=comms.messaging.user_identity(sys.argv[1]); "
                    "comms.bus.reads.read(OneViewer(viewer.incarnation)); os._exit(9)",
                    str(tmp_path),
                ],
                env=dict(os.environ),
                capture_output=True,
                timeout=15,
            )
            assert child.returncode == 9, child.stderr.decode()
            comms = wire(tmp_path)
        seen = displayed_sequences(comms, viewer)
        assert seen <= current_sequences(painted), (seed, index, operation)
        assert seen == current_sequences(acknowledged), (seed, index, operation)
