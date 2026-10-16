"""Read ownership: sparse paint, real conversation prefixes and typed boundaries."""
from dataclasses import replace
import pytest
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, InfoKind
from agent_comms.read_basis import Conversation
from agent_comms.read_ledger import ConversationKey, ReadMask, ReadProgress
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread


def test_interleaved_global_sequences_do_not_grant_a_conversation_gap(tmp_path):
    comms = wire(tmp_path)
    for name in ('sender', 'noise'):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    first = comms.messaging.send_message('sender', viewer, 'first')
    comms.messaging.send_message('noise', '#other', 'outside conversation')
    middle = comms.messaging.send_message('sender', viewer, 'middle')
    comms.messaging.send_message('noise', '#other', 'outside conversation again')
    last = comms.messaging.send_message('sender', viewer, 'last')
    snapshot = comms.registry.snapshot()
    for message, unread in ((last, 2), (first, 1), (middle, 0)):
        comms.bus.reads.mark_displayed(viewer, comms.bus.reads.capture(
            viewer, (message,), snapshot, comms.bus.log.path), snapshot=snapshot)
        assert comms.bus.pending_count(viewer, 'sender') == unread
    state, = comms.bus.reads.read().messages.values()
    assert state.read_through == last.seq and not state.sparse


def test_typed_conversation_keys_decode_once_and_reject_duplicates():
    key = ConversationKey(ThreadIncarnation('viewer', 1.0), Conversation(target='#team'))
    states = {key: ReadProgress(4, frozenset({8}))}
    encoded = FieldCodec.encode(states)
    assert FieldCodec.decode(dict[ConversationKey, ReadProgress], encoded) == states
    with pytest.raises(ValueError, match='Duplicate'):
        FieldCodec.decode(dict[ConversationKey, ReadProgress], [encoded[0], encoded[0]])


def test_message_membership_is_one_conversation_lookup_without_iteration():
    calls = []
    class LookupOnly(dict):
        def get(self, key, default=None):
            calls.append(key)
            return super().get(key, default)
        def __iter__(self):
            raise AssertionError('No per-message conversation scan')
        def items(self):
            raise AssertionError('No per-message conversation scan')
    conversation = Conversation(target='#team')
    mask = ReadMask(LookupOnly({conversation: ReadProgress(10)}))
    message = replace(Message('sender', '#team', 'painted', InfoKind), seq=5)
    assert mask.contains(message)
    assert calls == [conversation]
