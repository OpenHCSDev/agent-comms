"""Selected check history is small, non-executable and scoped to the command."""


from agent_comms.repo import Checkout
import json
from pathlib import Path
import sys

import pytest
from bus_record_fixture import append_reply

from agent_comms.check_root import CheckRoot
from agent_comms.child_process import NoProcess
from agent_comms.cli import main
from agent_comms.comms import Comms, wire
from agent_comms.errors import UnreceivedAnswerError
from agent_comms.messaging import CommanderReceipts
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.transcript_events import AssistantTranscript, NoticeTranscript, UserTranscript
from agent_comms.native_entries import NativeEntry
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread
from agent_comms.turn_item import NativeItemId, NativeTurnId


@pytest.fixture
def source(scratch_root, durable_home):
    root = scratch_root / "source"
    c = wire(root)
    for name in ("A", "B", "other"):
        c.registry.declare(Thread(name, frozenset({"comms"}), Checkout(scratch_root)),
                           StoppedThreadStatus())
    c.messaging.send("A", "B", "old history is not a new input")
    c.registry.register(c.registry.require("other"), RunningThreadStatus())
    c.registry.rename("other", "unselected")
    return c




@pytest.mark.parametrize("timestamp", (None, 1_760_000_000.125))
def test_external_projection_preserves_event_clocks_or_refuses_unknown(source, scratch_root, timestamp):
    saved_turn_id = NativeTurnId('saved')
    owner = source.registry.require("B")
    external = ExternalTranscript(source.root, owner.incarnation)
    events = (
        UserTranscript("source request " * 1500, position=1, timestamp=timestamp),
        AssistantTranscript("source reply", position=2,
                            timestamp=None if timestamp is None else timestamp + 1.125),
        NoticeTranscript("not portable conversation", position=3, timestamp=timestamp),
        UserTranscript("", position=4, timestamp=timestamp),
    )
    root = scratch_root / "external-window"
    if timestamp is None:
        # Current records require a clock at their storage boundary, before copying.
        with pytest.raises(TypeError):
            external.finish(turn_id=saved_turn_id, item_id=NativeItemId("item"), events=events)
        with external.reading() as original:
            assert not original.has_records(original.cut().offset)
        assert not root.exists()
        return
    external.finish(turn_id=saved_turn_id, item_id=NativeItemId("item"), events=events)
    with CheckRoot(source, (owner.name,), root).open() as copied:
        session = Path(copied.registry.require(owner.name).runtime.path)
        rows = [NativeEntry.read(line) for line in session.read_bytes().splitlines()]
        assert [entry.message.text for entry in rows[1:]] == [event.text for event in events[:2]]
        assert [entry.timestamp.at.timestamp() for entry in rows[1:]] == [
            event.timestamp for event in events[:2]
        ]
    with external.reading() as original:
        assert tuple(original.reverse(original.cut().offset))[0].events == events
    assert not root.exists()


def test_check_roots_contain_new_child_allocations_and_actual_rule_publication(
    source: Comms, scratch_root: Path,
):
    from agent_comms.pi_rule_gate import PiRedeal
    from agent_comms.rule_deal import StartedDeal
    from agent_comms.thread_text import ForkedParent

    source_policy = source.registry.storage_rule
    scratches = []
    for label in ("first-copy", "second-copy"):
        root = scratch_root / label
        with CheckRoot(source, ("A",), root).open() as copied:
            # Another process/client must load the persisted policy, not a patched Registration.
            reopened = wire(copied.root)
            parent = reopened.registry.require("A")
            child = reopened.registry.declare_child(parent, lambda current: Thread(
                "fork", frozenset({"comms"}), current.worktree,
                parent=ForkedParent(current.name)))
            allocation = reopened.registry.storage_rule.roots_of(child)
            assert allocation.scratch.is_relative_to(root)
            assert reopened.registry.storage_rule.checkout_base.is_relative_to(root)
            assert reopened.registry.storage_rule.roots_of(parent).scratch.is_relative_to(root)
            gate = PiRedeal.of(reopened, child)
            assert gate.stem.is_relative_to(allocation.scratch)
            gate.redeal(StartedDeal())
            assert gate.refreshed.is_file()
            assert gate.record.is_file()
            assert gate.audit.is_relative_to(root)
            scratches.append(allocation.scratch)
            assert source.registry.storage_rule == source_policy
        assert not root.exists()
    assert scratches[0] != scratches[1]


def test_wrapped_nonzero_exit_removes_root_and_is_the_cli_exit(source, scratch_root, capsys):
    root = scratch_root / "command"
    status = main(["--root", str(source.root), "check-root", "--threads", "B", "--into", str(root),
                   "--", sys.executable, "-c",
                   "import pathlib,sys; assert pathlib.Path(sys.argv[1]).is_dir(); sys.exit(7)", "{root}"])
    assert status == 7
    assert json.loads(capsys.readouterr().out)["return_code"] == 7
    assert not root.exists()


def test_bounded_check_history_keeps_the_original_answered_before_its_tail(source, scratch_root):
    original = source.bus.bus.lookup(1)
    for index in range(CheckRoot.max_records - 1):
        source.messaging.send("A", "B", f"Recent context {index}")
    reply = source.messaging.send("B", "A", "Answer the older question", answers=(original.reference,)).message
    root = scratch_root / "linked-window"
    with CheckRoot(source, ("B",), root).open() as copied:
        assert len(tuple(copied.bus.bus.message_history())) == CheckRoot.max_records
        assert copied.bus.bus.lookup(original.seq).reference == original.reference
        assert copied.bus.bus.lookup(reply.seq).answers == (original.reference,)
        assert tuple(message.reference for message in copied.bus.bus.replies(
            (original.reference,),
        )) == (reply.reference,)
    assert not root.exists()


def test_check_history_never_pulls_an_answered_message_from_outside_its_scope(source, scratch_root):
    # unit-busdel's check root refused on live answer 23164, whose original 22950 was a DM
    # between two other threads: the copy keeps the answer and names no undisclosed message.
    # Send now refuses this answer; the record stands for ones appended before it did.
    original = source.messaging.send("unselected", "A", "Unselected private context").message
    people = source.registry.snapshot()
    reply = append_reply(
        source.bus.bus, people.require("B").incarnation.require_recorded(),
        people.require("A").incarnation.require_recorded(),
        "A reference does not expand the selected scope", answers=(original.reference,))
    root = scratch_root / "outside-window"
    before = source.bus.bus.revision()
    with CheckRoot(source, ("B",), root).open() as copied:
        history = tuple(copied.bus.bus.message_history())
        assert original.seq not in {message.seq for message in history}
        assert copied.bus.bus.lookup(reply.seq).answers == ()
        assert all(thread.process_identity == NoProcess()
                   for thread in copied.registry.snapshot().threads.values())
    assert source.bus.bus.revision() == before
    assert not root.exists()


def test_a_message_answers_only_what_its_sender_received(source):
    # Live 23164: unit-busdel answered 22950, fork-ledgerfold's DM to unit-installer.
    private = source.messaging.send("unselected", "A", "Unselected private context").message
    with pytest.raises(UnreceivedAnswerError, match=f"B never received {private.message_id}"):
        source.messaging.send("B", "A", "Not mine to answer", answers=(private.reference,))
    source.channels.create_tag("comms")
    channel = source.messaging.send("A", "#comms", "A channel question").message
    for sender, original in (("A", private), ("B", channel)):
        reply = source.messaging.send(sender, "unselected", "Answered",
                                      answers=(original.reference,)).message
        assert source.bus.bus.lookup(reply.seq).answers == (original.reference,)


def test_acting_for_a_commander_answers_what_that_commander_received(source):
    # 10-30: 35 asks owed by retired codex-greentext refused every successor and its forks
    # because the gate asked "did you receive it" instead of "do you act for who did".
    retired_ask = source.messaging.send("unselected", "A", "Ask the retired commander").message
    people = source.registry.snapshot()
    retired = people.require("A").incarnation.require_recorded()
    peer = people.require("B").incarnation.require_recorded()
    peer_authority = peer.peer_for(peer)
    fork = people.require("unselected").incarnation.require_recorded().acting_for(peer_authority)
    for actor in (peer_authority, fork):
        reply = source.messaging.send(
            "B", "unselected", "Answered for the retired commander",
            answers=(retired_ask.reference,),
            answerer=CommanderReceipts(actor, (retired,))).message
        assert source.bus.bus.lookup(reply.seq).answers == (retired_ask.reference,)
    with pytest.raises(UnreceivedAnswerError, match=f"B never received {retired_ask.message_id}"):
        source.messaging.send("B", "unselected", "Not my commander",
                              answers=(retired_ask.reference,),
                              answerer=CommanderReceipts(peer, (retired,)))


def test_a_check_root_answers_a_dm_received_before_the_copy(source, scratch_root):
    # A copy written as public records had no receipts, so send refused every old answer.
    original = source.bus.bus.lookup(1)
    with CheckRoot(source, ("A", "B"), scratch_root / "received").open() as copied:
        reply = copied.messaging.send("B", "A", "Answered inside the copy",
                                      answers=(original.reference,)).message
        assert copied.bus.bus.lookup(reply.seq).answers == (original.reference,)
