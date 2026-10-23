from dataclasses import replace

from agent_comms.channel_targets import CommandTarget
from agent_comms.read_basis import ChannelConversation, ConversationCursor, ReadCursor
from agent_comms.read_ledger import ReadDocument
from agent_comms.recorded_bus import RecordedBusIdentity
from conftest import installed_comms
from agent_comms.threads import Thread


def test_prefixes_and_alias_union_stay_in_their_conversation():
    a, b = ChannelConversation(target="#a"), ChannelConversation(target="#b")
    first = ReadCursor({a: ConversationCursor(100)})
    second = ReadCursor({b: ConversationCursor(10, (50,))})
    joined = ReadCursor.union((first, second))
    assert 50 in joined.for_conversation(a)
    assert 40 not in joined.for_conversation(b)
    assert 50 in joined.for_conversation(b)
    assert joined.for_conversation(b).high_water == 10
    assert not hasattr(joined, "high_water")


def test_sparse_prefix_drops_only_its_covered_exceptions():
    cursor = ConversationCursor(3, (20, 40, 60))
    assert cursor.through(40) == ConversationCursor(40, (60,))
    assert cursor.add((30, 20, 5)) == ConversationCursor(3, (5, 20, 30, 40, 60))


def test_in_order_additions_become_one_prefix():
    cursor = ConversationCursor()
    for sequence in range(1, 10_001):
        cursor = cursor.add((sequence,))
    assert cursor.high_water == 10_000
    assert cursor.exceptions == ()


def test_add_closes_gaps_without_losing_sparse_reads_or_mutating_the_source():
    original = ConversationCursor(3, (7, 9, 12))
    cursor = original.add((6, 4, 11, 5, 11, 2))
    assert cursor == ConversationCursor(7, (9, 11, 12))
    assert cursor.add((8, 10)) == ConversationCursor(12)
    assert original == ConversationCursor(3, (7, 9, 12))
    assert cursor.through(8) == ConversationCursor(8, (9, 11, 12))
    assert ConversationCursor.union((ConversationCursor(3, (5, 7)),
                                     ConversationCursor(4, (6, 9)))) == ConversationCursor(
                                         4, (5, 6, 7, 9))


def setup_conversations(root):
    comms = installed_comms(root)
    for name in ("sender", "reader"):
        comms.registry.declare(Thread(
            name, frozenset({"a", "b"}), str(root),
        ))
    messages = [comms.messaging.send("sender", target, target).message
                for target in ("#a", "#b", "#a", "#b")]
    return comms, messages


def seed_prefix(comms, viewer, through):
    snapshot = comms.registry.snapshot()
    owner = snapshot.threads[viewer].incarnation
    comms.bus.reads.replace(replace(
        comms.bus.reads.read(),
        messages={owner: ReadCursor({ChannelConversation(target="#a"): ConversationCursor(through)})},
        bus_identity=RecordedBusIdentity.capture(comms.bus.bus.path),
    ))


def test_real_route_counts_and_inbox_use_each_conversation(tmp_path):
    comms, messages = setup_conversations(tmp_path)
    seed_prefix(comms, "reader", messages[2].seq)
    assert comms.bus.pending_count("reader", CommandTarget.named("#a")) == 0
    assert comms.bus.pending_count("reader", CommandTarget.named("#b")) == 2
    assert [m.seq for m in comms.bus.inbox("reader")] == [messages[1].seq, messages[3].seq]
    basis = comms.bus.delivered("reader", tuple(m.reference for m in messages))
    assert tuple(seq for item in basis.conversations for seq in item.sequences) == (
        messages[1].seq, messages[3].seq,
    )


def test_durable_cursor_map_round_trip():
    from agent_comms.field_codec import FieldCodec
    from agent_comms.thread_identity import RecordedThreadIncarnation

    owner = RecordedThreadIncarnation("reader", 1.0)
    doc = ReadDocument(messages={owner: ReadCursor({
        ChannelConversation(target="#a"): ConversationCursor(100),
        ChannelConversation(target="#b"): ConversationCursor(0, (50,)),
    })})
    assert FieldCodec.decode(ReadDocument, FieldCodec.encode(doc)) == doc
