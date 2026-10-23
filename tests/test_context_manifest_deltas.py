"""Recorded context reuses source-bound text and transcript prefixes."""
from agent_comms.field_codec import TurnId
from agent_comms.context_store import UNREAD_CONTEXT
from dataclasses import replace
import hashlib
import sys

import pytest

from agent_comms.context_store import ContextRequest
import json

from agent_comms.field_codec import FieldCodec
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.thread_identity import RegisteredTurn, TurnIdentity
from agent_comms.turn_context import ObservedDispatchRequest
from agent_comms.turn_context import (
    ContextManifest, JournalProvenance, NativeProvenance, RecordedContextPredecessor,
    RecordedContextTurn,
    SegmentManifest, SystemLayerSegment, ToolCatalogSegment, TranscriptSegment,
)
from bus_record_fixture import context_comms


def context(owner, path, count, turn):
    native = NativeSessionIdentity('manifest-delta', str(path / 'history.jsonl'))
    provenance = (NativeProvenance(native, 1, 'a' * 64),)
    def segment(kind, text, sources=provenance, contributors=()):
        raw = text.encode()
        return SegmentManifest(kind, sources, hashlib.sha256(raw).hexdigest(),
                               len(raw), 100, contributors=contributors, captured_text=(text,))
    contributors = tuple(segment(TranscriptSegment, f'Entry {i}',
        (*provenance, JournalProvenance(native.session_file, (f'entry-{i}',))))
        for i in range(count))
    transcript = segment(TranscriptSegment, '\n'.join(f'Entry {i}' for i in range(count)),
                         contributors=contributors)
    return ContextManifest(owner.incarnation,
        RecordedContextTurn(RegisteredTurn(TurnId(f"{turn:032x}")), TurnIdentity(owner.incarnation, turn)),
        (segment(SystemLayerSegment, 's' * 27000),
         segment(ToolCatalogSegment, 't' * 64000), transcript), 'counter',
        ObservedDispatchRequest(f'request-{turn}'))


def test_context_deltas_restore_original_text_and_incremental_membership(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 328, 1)
    second = context(owner, tmp_path, 330, 2)
    prior = UNREAD_CONTEXT
    for manifest in (first, second, replace(second, turn=replace(second.turn, identity=RegisteredTurn(TurnId('431ad49b3e0129a11ea5a0320c188024'))),
                                          request_id=ObservedDispatchRequest('request-3'))):
        # The writer keeps the request it recorded as the next request's prior.
        prior = comms.contexts.record_after(prior, manifest)
    page = comms.contexts.page(owner.name, comms.registry)
    assert len(page.resources) == 3
    decoded = [resource.recorded for resource in page.resources]
    assert len(json.dumps(FieldCodec.encode(decoded[1]))) < len(json.dumps(FieldCodec.encode(decoded[0]))) / 10
    assert decoded[1].segments[2].inherited_entries == 328
    assert len(decoded[1].segments[2].entries) == 2
    assert decoded[2].segments[2].previous == decoded[1].segments[2].previous
    for expected, actual in zip((first, second), (r.value for r in page.resources)):
        assert actual.segments[:2] == expected.segments[:2]
        assert actual.segments[2].contributors == expected.segments[2].contributors
        assert actual.segments[2].recorded_public_text({}) == expected.segments[2].recorded_public_text({})
    # Independent readers and retained-page readers resolve the same original values.
    assert comms.contexts.page(owner.name, comms.registry).resources == page.resources
    assert comms.contexts.page(owner.name, comms.registry,
                               previous=page.resources).resources == page.resources
    assert FieldCodec.decode(type(decoded[1]), FieldCodec.encode(decoded[1])) == decoded[1]


def test_changed_text_and_replaced_transcript_do_not_reuse_old_values(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 3, 1)
    comms.contexts.record_after(UNREAD_CONTEXT, first)
    second = context(owner, tmp_path, 1, 2)
    text = replace(second.segments[0], sha256='b' * 64, captured_text=('Changed system',))
    second = replace(second, segments=(text, *second.segments[1:]))
    comms.contexts.record_after(UNREAD_CONTEXT, second)
    page = comms.contexts.page(owner.name, comms.registry)
    assert page.resources[-1].value.segments[0].captured_text == ('Changed system',)
    assert len(page.resources[-1].value.segments[2].contributors) == 1


def test_newly_captured_text_is_not_replaced_by_an_empty_predecessor(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 1, 1)
    first = replace(first, segments=(replace(first.segments[0], captured_text=()),
                                     *first.segments[1:]))
    comms.contexts.record_after(UNREAD_CONTEXT, first)
    second = context(owner, tmp_path, 1, 2)
    comms.contexts.record_after(UNREAD_CONTEXT, second)
    page = comms.contexts.page(owner.name, comms.registry)
    assert page.resources[-1].value.segments[0].captured_text == second.segments[0].captured_text


def test_request_provenance_does_not_change_immutable_entry_membership(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 328, 1)
    comms.contexts.record_after(UNREAD_CONTEXT, first)
    second = context(owner, tmp_path, 330, 2)
    transcript = second.segments[2]
    contributors = tuple(replace(child, provenance=tuple(
        replace(source, request_generation=2, context_digest='b' * 64)
        if isinstance(source, NativeProvenance) else source for source in child.provenance))
        for child in transcript.contributors)
    second = replace(second, segments=(*second.segments[:2],
                                       replace(transcript, contributors=contributors)))
    comms.contexts.record_after(UNREAD_CONTEXT, second)
    page = comms.contexts.page(owner.name, comms.registry)
    actual = page.resources[-1]
    recorded = actual.recorded
    assert len(actual.value.segments[2].contributors) == 330
    assert recorded.segments[2].inherited_entries == 328
    assert len(recorded.segments[2].entries) == 2
    assert len(json.dumps(FieldCodec.encode(recorded))) < 3000
    assert actual.value.segments[2].recorded_public_text({}) == transcript.recorded_public_text({})


def test_forty_turns_use_direct_sources_and_fixed_transcript_keyframes(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    expected = [context(owner, tmp_path, 328 + 2 * turn, turn + 1) for turn in range(40)]
    for manifest in expected:
        comms.contexts.record_after(UNREAD_CONTEXT, manifest)
    from agent_comms.context_store import ContextStoreInfo
    with comms.contexts.snapshot() as db:
        source = ContextStoreInfo.one(db, singleton=1).source
        rows = ContextRequest.select(db, order_by=('id',))
        for turn in (1, 32, 33, 40):
            row = rows[turn - 1]
            result = comms.contexts.resolve(db, source, row)
            assert result.value.segments[:2] == expected[turn - 1].segments[:2]
            assert result.value.segments[2].contributors == expected[turn - 1].segments[2].contributors
            recorded = result.recorded
            assert recorded.segments[0].predecessors(row.id) == (
                () if turn == 1 else (rows[0].id,))
            assert recorded.segments[2].predecessors(row.id) == (
                () if turn in (1, 32) else (rows[31].id,))
        decoded = []
        previous_profile = sys.getprofile()
        def observe(frame, event, argument):
            if event == 'call' and frame.f_code is ContextRequest.recorded.__code__:
                decoded.append(frame.f_locals['self'].id)
        try:
            sys.setprofile(observe)
            comms.contexts.resolve(db, source, rows[-1])
        finally:
            sys.setprofile(previous_profile)
        assert len(decoded) == len(set(decoded)) == 3
        last, delta = rows[-1].recorded(db), rows[32].recorded(db)
        with pytest.raises(ValueError):
            replace(last.segments[2], previous=RecordedContextPredecessor(rows[32].id)).restore(
                {}, {rows[32].id: delta}, position=2)


def test_missing_store_identity_refuses_reads_and_further_records(tmp_path):
    from agent_comms.context_store import ContextStoreInfo
    from agent_comms.errors import RelationViolationError

    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    original = context(owner, tmp_path, 2, 1)
    comms.contexts.record_after(UNREAD_CONTEXT, original)
    with comms.contexts.writing() as db:
        ContextStoreInfo.delete(db, where='singleton=1')
    with pytest.raises(RelationViolationError):
        comms.contexts.page('Alice', comms.registry)
    with pytest.raises(RelationViolationError):
        comms.contexts.record_after(UNREAD_CONTEXT, original)
    with comms.contexts.snapshot() as db:
        assert not ContextStoreInfo.exists(db)
        assert ContextRequest.count(db) == 1


def test_threads_record_concurrently_and_a_superseded_prior_is_refused(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from agent_comms.context_store import ContextPriorSupersededError

    comms = context_comms(tmp_path)
    owners = [comms.registry.require(name) for name in ('Alice', 'Bob')]
    priors = [comms.contexts.prior(owner.incarnation) for owner in owners]
    with ThreadPoolExecutor(2) as pool:
        recorded = list(pool.map(lambda pair: comms.contexts.record_after(
            pair[0], context(pair[1], tmp_path, 3, 1)), zip(priors, owners)))
    alice = recorded[0]
    following = comms.contexts.record_after(alice, context(owners[0], tmp_path, 4, 2))
    assert following.recorded.recording_turn == alice.recorded.recording_turn + 1
    assert comms.contexts.page('Bob', comms.registry).resources[-1].record_id == recorded[1].record_id
    with pytest.raises(ContextPriorSupersededError):
        comms.contexts.record_after(alice, context(owners[0], tmp_path, 5, 3))
