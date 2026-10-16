"""Read ownership: sparse paint, real conversation prefixes and typed boundaries."""
from dataclasses import replace
import pytest
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, InfoKind
from agent_comms.read_basis import Conversation
from agent_comms.read_ledger import (ConversationKey, ReadMask, ReadProgress,
    ReadPrefix, PrefixUnavailable, SelectedViewers, OneViewer, ReadRevision,
    PresentReadRevision, AbsentReadRevision)
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
    state, = comms.bus.reads.read(OneViewer(snapshot.participant(viewer).incarnation)).messages.values()
    assert state.read_through == last.seq and not state.sparse


def test_typed_conversation_keys_decode_once_and_reject_duplicates():
    key = ConversationKey(ThreadIncarnation('viewer', 1.0), Conversation(target='#team'))
    states = {key: ReadProgress(4, frozenset({8}))}
    encoded = FieldCodec.encode(states, dict[ConversationKey, ReadProgress])
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


def test_prefix_family_never_grants_unproven_sparse_paint():
    state = ReadProgress(1, frozenset((4,)))
    assert state.mark((6,), lambda _: PrefixUnavailable()) == ReadProgress(1, frozenset((4, 6)))
    assert state.mark((6,), lambda _: ReadPrefix(4)) == ReadProgress(4, frozenset((6,)))


def test_progress_owns_empty_frontier_without_requesting_index_proof():
    def unexpected(_progress):
        raise AssertionError('An empty sparse frontier must not request an index proof')

    state = ReadProgress(4)
    assert state.compact(unexpected) is state
    assert state.mark((1, 2, 4), unexpected) == state


def test_display_basis_owns_empty_selected_paint_without_opening_authority(tmp_path):
    from agent_comms.read_basis import DisplayBasis, DisplayedConversation

    comms = wire(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path))
    conversation = Conversation(target='#team')
    shown = DisplayBasis(viewer.name, viewer.created_at,
                         (DisplayedConversation(conversation, (1,)),), None)
    assert shown
    for empty in (shown.select(()), shown.through(0)):
        assert not empty
        assert empty.viewer_identity == viewer.incarnation
        assert empty.bus_identity is None
        assert not empty.conversations[0]
        comms.bus.reads.mark_displayed(viewer.name, empty, snapshot=comms.registry.snapshot())
        assert not comms.bus.reads.path.exists()


def test_undeclared_dictionary_is_not_guessed_from_its_values():
    for value in ({}, {'a': 1}, {1: 2}):
        with pytest.raises(TypeError, match='declared dict'):
            FieldCodec.encode(value)
    assert FieldCodec.encode([{'a': 1}, {'b': 2}], list[dict[str, int]]) == [{'a': 1}, {'b': 2}]


def test_missing_incarnation_is_not_a_deleted_identity():
    with pytest.raises(KeyError):
        ReadMask().incarnation('not-in-this-mask')
    assert not ReadMask().contains(replace(Message('a', 'b', 'unseen', InfoKind), seq=1))


def test_revision_family_roundtrip_and_explicit_viewer_scope(tmp_path):
    assert isinstance(ReadRevision.capture((tmp_path / 'absent',)), AbsentReadRevision)
    path = tmp_path / 'present'
    path.write_text('owned')
    revision = ReadRevision.capture((path,))
    assert isinstance(revision, PresentReadRevision)
    assert FieldCodec.decode(ReadRevision, FieldCodec.encode(revision)) == revision
    assert SelectedViewers((ThreadIncarnation('viewer', 1.0),)).incarnations == (ThreadIncarnation('viewer', 1.0),)
    assert OneViewer(ThreadIncarnation('viewer', 1.0)).incarnation.name == 'viewer'


def test_required_authority_and_header_cannot_look_like_empty_state(tmp_path):
    from agent_comms.read_ledger import (
        AbsentReadSource, NoReadNotice, ReadHeaderRow, ReadLedger,
    )

    ledger = ReadLedger(tmp_path)
    with pytest.raises(FileNotFoundError):
        ledger.read(SelectedViewers(()))
    ledger.create(AbsentReadSource(), NoReadNotice())
    assert ledger.read(SelectedViewers(())).messages == {}
    with pytest.raises(FileExistsError):
        ledger.create(AbsentReadSource(), NoReadNotice())
    with ledger.writing() as connection:
        ReadHeaderRow.delete(connection)
    with pytest.raises(ValueError):
        ledger.read(SelectedViewers(()))


def test_missing_reader_is_typed_and_does_not_insert_a_second_record():
    from agent_comms.read_ledger import NothingRead, ReadMasks

    masks = ReadMasks()
    absent = masks['viewer']
    assert isinstance(absent, NothingRead)
    assert absent.progress('sender', '#team') == ReadProgress()
    assert not absent.sparse
    assert not absent.contains_route('sender', '#team', 1)
    assert masks == {}
    # Disposable display checkpoints decode the declared base value. Missing
    # membership must not invalidate an otherwise warm projection every time.
    restored = FieldCodec.decode(ReadMask, FieldCodec.encode(absent))
    assert restored == absent == ReadMask()
    assert absent == restored
    with pytest.raises(TypeError):
        NothingRead({'not': 'a recorded mask'})


def test_source_absence_and_mismatch_return_the_same_typed_empty_membership():
    from agent_comms.private_path import FileIdentity
    from agent_comms.read_ledger import (
        AbsentReadSource, NothingRead, PresentReadSource, ReadMasks, ReadObservations,
    )

    source = PresentReadSource(FileIdentity(1, 2))
    mask = ReadMask({Conversation(target='#team'): ReadProgress(3)})
    captured = ReadObservations(source, ReadMasks({'viewer': mask}))
    assert captured.for_source(source)['viewer'] is mask
    for other in (AbsentReadSource(), PresentReadSource(FileIdentity(1, 3))):
        assert isinstance(captured.for_source(other)['viewer'], NothingRead)


def test_ledger_returns_nothing_read_for_a_known_viewer_without_a_record(tmp_path):
    from agent_comms.read_ledger import AbsentReadSource, NoReadNotice, NothingRead

    comms = wire(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path))
    snapshot = comms.registry.snapshot()
    source = AbsentReadSource()
    # No message source exists yet. The fixture explicitly creates authority;
    # a missing authority/header is not the same as a missing reader record.
    comms.bus.reads.create(source, NoReadNotice())
    assert isinstance(comms.bus.reads.membership(viewer.name, snapshot, source), NothingRead)
