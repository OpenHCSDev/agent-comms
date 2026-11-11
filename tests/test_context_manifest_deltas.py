"""Recorded context reuses source-bound text and transcript prefixes."""
import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import get_type_hints

import pytest

from agent_comms.context_segments.native_messages import TranscriptSegment
from agent_comms.context_store import (
    UNREAD_CONTEXT,
    CapturedContext,
    ContextEntry,
    ContextManifestReference,
    RelationViolationError,
    ContextRequest,
    ContextSegmentVersion,
    ContextStore,
    ContextStoreIdentity,
    ContextStoreInfo,
)
from agent_comms.errors import RelationViolationError
from agent_comms.execution.native_session_reopen import NativeSessionIdentity, PiSessionId
from agent_comms.execution.turn_context import (
    ContextManifest,
    ContextRecording,
    JournalProvenance,
    NativeProvenance,
    ObservedDispatchRequest,
    RecordedContextManifest,
    RecordedContextPredecessor,
    RecordedContextTurn,
    RecordedSegmentManifest,
    SegmentManifest,
    SystemLayerSegment,
    ToolCatalogSegment,
)
from agent_comms.execution.turn_item import PiRequestKey
from agent_comms.installer.practice import PracticeCopy
from agent_comms.nominal.field_codec import ContextStoreId, FieldCodec, TurnId
from agent_comms.registry.thread_identity import TurnIdentity
from agent_comms.storage.typed_table import RowNotFoundError, SQLitePageSize
from bus_record_fixture import context_comms


def test_manifest_reads_the_original_recording_contract() -> None:
    assert get_type_hints(RecordedContextManifest.capture)["previous"] is ContextRecording
    assert issubclass(CapturedContext, ContextRecording)


def context(owner, path, count, turn):
    native = NativeSessionIdentity(PiSessionId('manifest-delta'), str(path / 'history.jsonl'))
    provenance = (NativeProvenance(native, 1, 'a' * 64),)
    def segment(kind, text, sources=provenance, contributors=()):
        raw = text.encode()
        return SegmentManifest(kind=kind, provenance=sources, sha256=hashlib.sha256(raw).hexdigest(),
                               utf8_bytes=len(raw), tokens=100, contributors=contributors, captured_text=(text,))
    contributors = tuple(segment(TranscriptSegment, f'Entry {i}',
        (*provenance, JournalProvenance(native.session_file, (f'entry-{i}',))))
        for i in range(count))
    transcript = segment(TranscriptSegment, '\n'.join(f'Entry {i}' for i in range(count)),
                         contributors=contributors)
    return ContextManifest(
        thread=owner.incarnation,
        turn=RecordedContextTurn(
            TurnId(f"{turn:032x}"), TurnIdentity(owner.incarnation, turn)
        ),
        segments=(
            segment(SystemLayerSegment, 's' * 27000),
            segment(ToolCatalogSegment, 't' * 64000),
            transcript,
        ),
        counter='counter',
        request_id=ObservedDispatchRequest(PiRequestKey(f'request-{turn}')),
    )


def assert_restores(actual, expected):
    """A restored manifest returns the recorded segments, the transcript's contributors and
    its public text; its flat captured text comes back through the entries, not inline."""
    assert actual.segments[:2] == expected.segments[:2]
    assert actual.segments[2].contributors == expected.segments[2].contributors
    assert actual.segments[2].recorded_public_text({}) == expected.segments[2].recorded_public_text({})


def test_context_deltas_restore_original_text_and_incremental_membership(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 328, 1)
    second = context(owner, tmp_path, 330, 2)
    prior = UNREAD_CONTEXT
    for manifest in (
        first,
        second,
        replace(
            second,
            turn=replace(
                second.turn, identity=TurnId('431ad49b3e0129a11ea5a0320c188024')
            ),
            request_id=ObservedDispatchRequest(PiRequestKey('request-3')),
        ),
    ):
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
        assert_restores(actual, expected)
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
    with comms.contexts.snapshot() as db:
        source = ContextStoreInfo.one(db, singleton=1).source
        rows = ContextRequest.select(db, order_by=('id',))
        for turn in (1, 32, 33, 40):
            row = rows[turn - 1]
            result, = comms.contexts.resolve(db, source, (row,))
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
            tuple(comms.contexts.resolve(db, source, (rows[-1],)))
        finally:
            sys.setprofile(previous_profile)
        assert len(decoded) == len(set(decoded)) == 3
        last, delta = rows[-1].recorded(db), rows[32].recorded(db)
        with pytest.raises(ValueError):
            replace(last.segments[2], previous=RecordedContextPredecessor(rows[32].id)).restore(
                {}, {rows[32].id: delta}, position=2)


def test_context_batch_decodes_shared_entries_once(tmp_path: Path) -> None:
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    expected = tuple(context(owner, tmp_path, count, turn)
                     for turn, count in enumerate((3, 4, 5), start=1))
    prior = UNREAD_CONTEXT
    for manifest in expected:
        prior = comms.contexts.record_after(prior, manifest)
    decoded = []
    with comms.contexts.snapshot() as db:
        source = ContextStoreInfo.require(db, singleton=1).source
        rows = ContextRequest.select(db, order_by=('id',))
        previous_profile = sys.getprofile()
        def observe(frame, event, argument):
            if event == 'call' and frame.f_code is ContextEntry.original.__func__.__code__:
                decoded.append(frame.f_locals['reference'])
        try:
            sys.setprofile(observe)
            actual = tuple(comms.contexts.resolve(db, source, rows))
        finally:
            sys.setprofile(previous_profile)
    assert len(decoded) == len(set(decoded)) == 5
    for original, restored in zip(expected, actual, strict=True):
        assert_restores(restored.value, original)


@pytest.mark.parametrize('preloaded', (False, True))
def test_context_batch_refuses_foreign_predecessor_even_when_preloaded(
    tmp_path: Path, preloaded: bool,
) -> None:
    comms = context_comms(tmp_path)
    alice, bob = (comms.registry.require(name) for name in ('Alice', 'Bob'))
    foreign = comms.contexts.record_after(UNREAD_CONTEXT, context(bob, tmp_path, 1, 1))
    original = comms.contexts.record_after(UNREAD_CONTEXT, context(alice, tmp_path, 1, 1))
    record = replace(original.recorded, segments=(
        replace(original.recorded.segments[0], previous=RecordedContextPredecessor(foreign.record_id)),
        *original.recorded.segments[1:],
    ))
    with comms.contexts.writing() as db:
        forged = ContextRequest.append(db, record)
    with comms.contexts.snapshot() as db:
        selected = ContextRequest.require(db, id=forged)
        rows = ((ContextRequest.require(db, id=foreign.record_id), selected)
                if preloaded else (selected,))
        with pytest.raises(RelationViolationError, match='predecessor is missing or foreign'):
            tuple(comms.contexts.resolve(db, original.source, rows))


def test_context_batch_keeps_same_session_and_entry_on_different_paths(tmp_path: Path) -> None:
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    originals = tuple(comms.contexts.record_after(
        UNREAD_CONTEXT, context(owner, tmp_path / path, 1, turn),
    ) for turn, path in enumerate(('one', 'two'), start=1))
    with comms.contexts.snapshot() as db:
        rows = ContextRequest.select(db, order_by=('id',))
        actual = tuple(comms.contexts.resolve(db, originals[0].source, rows))
    for original, restored in zip(originals, actual, strict=True):
        assert (restored.source, restored.record_id, restored.recorded) == (
            original.source, original.record_id, original.recorded)
        assert_restores(restored.value, original.value)


def test_missing_store_identity_refuses_reads_and_further_records(tmp_path):

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
    with pytest.raises(RelationViolationError):
        comms.contexts.record_after(alice, context(owners[0], tmp_path, 5, 3))


def test_selected_context_copy_preserves_closure_and_original_sequences(tmp_path: Path) -> None:
    comms = context_comms(tmp_path / 'source')
    owner = comms.registry.require('Alice')
    originals = tuple(comms.contexts.record_after(
        UNREAD_CONTEXT, context(owner, tmp_path, count, turn),
    ) for turn, count in enumerate((1, 2, 4), start=1))
    selected = originals[1]
    reference = ContextManifestReference(selected.source, selected.record_id)
    with comms.contexts.snapshot() as db:
        expected, = comms.contexts.resolve(
            db, reference.source, (ContextRequest.require(db, id=reference.record_id),),
        )
    before = ContextStore.digest(comms.contexts.path)
    copy = PracticeCopy(comms.root, tmp_path / 'retained')
    copy.include_contexts((), requests=(reference, reference), maximum_bytes=2**20)
    retained = ContextStore(copy.path)
    comms.contexts.require_retained(retained, (), (reference,))

    with retained.snapshot() as db:
        info = ContextStoreInfo.require(db, singleton=1)
        assert info.source == selected.source
        rows = ContextRequest.select(db, order_by=('id',))
        assert tuple(row.id for row in rows) == tuple(row.record_id for row in originals[:2])
        actual, = retained.resolve(db, info.source, (rows[-1],))
        assert actual == expected
        assert ContextEntry.count(db) == 2
        retained_version = max(row.id for row in ContextSegmentVersion.select(db))
    with comms.contexts.snapshot() as db:
        source_version = max(row.id for row in ContextSegmentVersion.select(db))
        assert retained_version < source_version
    with retained.writing() as db:
        # Real row owners allocate after the full original high-water mark,
        # not after the greatest ID that happened to be in this selection.
        assert ContextRequest.append(db, selected.recorded) == originals[-1].record_id + 1
        fresh = RecordedSegmentManifest(context(owner, tmp_path, 6, 4).segments[2])
        assert ContextSegmentVersion.append(db, fresh) == source_version + 1
    assert ContextStore.digest(comms.contexts.path) == before
    assert ContextStore.path_in(comms.root) in copy.copied
    assert copy.request.source_root() == comms.root
    assert copy.request.root == copy.path


def test_selected_context_readback_refuses_omitted_native_entries(tmp_path: Path) -> None:
    comms = context_comms(tmp_path / 'source')
    owner = comms.registry.require('Alice')
    selected = comms.contexts.record_after(UNREAD_CONTEXT, context(owner, tmp_path, 1, 1))
    identity = selected.recorded.segments[2].entries[0].identity
    destination = ContextStore(tmp_path / 'retained')
    comms.contexts.retain_contexts(destination, (), maximum_bytes=2**20)
    with pytest.raises(RelationViolationError, match='context entries differ'):
        comms.contexts.require_retained(destination, (identity,), ())


def foreign_context_reference(
    store: ContextStore, previous: CapturedContext, reference: ContextManifestReference,
) -> ContextManifestReference:
    return replace(reference, source=ContextStoreIdentity(ContextStoreId.mint()))


def missing_context_reference(
    store: ContextStore, previous: CapturedContext, reference: ContextManifestReference,
) -> ContextManifestReference:
    return replace(reference, record_id=reference.record_id + 100)


def missing_context_predecessor(
    store: ContextStore, previous: CapturedContext, reference: ContextManifestReference,
) -> ContextManifestReference:
    with store.writing() as db:
        ContextRequest.delete(db, id=previous.record_id)
    return reference


@pytest.mark.parametrize(('change', 'expected'), (
    pytest.param(foreign_context_reference, RelationViolationError, id='foreign-source'),
    pytest.param(missing_context_reference, RowNotFoundError, id='missing-record'),
    pytest.param(missing_context_predecessor, RowNotFoundError, id='missing-predecessor'),
))
def test_selected_context_copy_refuses_without_publishing(
    tmp_path: Path,
    change: Callable[[ContextStore, CapturedContext, ContextManifestReference], ContextManifestReference],
    expected: type[Exception],
) -> None:
    comms = context_comms(tmp_path / 'source')
    owner = comms.registry.require('Alice')
    first = comms.contexts.record_after(UNREAD_CONTEXT, context(owner, tmp_path, 1, 1))
    second = comms.contexts.record_after(first, context(owner, tmp_path, 2, 2))
    reference = change(comms.contexts, first, ContextManifestReference(second.source, second.record_id))
    before = ContextStore.digest(comms.contexts.path)
    destination = ContextStore(tmp_path / 'retained')
    with pytest.raises(expected):
        comms.contexts.retain_contexts(
            destination, (), requests=(reference,), maximum_bytes=2**20,
        )
    assert not destination.path.exists()
    assert not tuple(destination.root.iterdir())
    assert ContextStore.digest(comms.contexts.path) == before


def test_selected_context_allowance_counts_existing_companions(tmp_path: Path) -> None:
    comms = context_comms(tmp_path / 'source')
    owner = comms.registry.require('Alice')
    selected = comms.contexts.record_after(UNREAD_CONTEXT, context(owner, tmp_path, 1, 1))
    copy = PracticeCopy(comms.root, tmp_path / 'retained')
    copy.prepare()
    companion = copy.path / 'earlier-store.sqlite3-wal'
    original_bytes = b'original retained companion'
    companion.write_bytes(original_bytes)
    before = ContextStore.digest(comms.contexts.path)
    with pytest.raises(RelationViolationError):
        copy.include_contexts((), maximum_bytes=len(original_bytes), requests=(
            ContextManifestReference(selected.source, selected.record_id),
        ))
    assert not ContextStore(copy.path).path.exists()
    assert tuple(copy.path.iterdir()) == (companion,)
    assert companion.read_bytes() == original_bytes
    assert not copy.copied
    assert ContextStore.digest(comms.contexts.path) == before


def test_selected_context_sqlite_allocation_limit_leaves_no_publication(tmp_path: Path) -> None:
    comms = context_comms(tmp_path / 'source')
    owner = comms.registry.require('Alice')
    selected = comms.contexts.record_after(UNREAD_CONTEXT, context(owner, tmp_path, 1, 1))
    copy = PracticeCopy(comms.root, tmp_path / 'retained')
    before = ContextStore.digest(comms.contexts.path)
    with comms.contexts.snapshot() as db:
        page, = SQLitePageSize.read(db.execute('PRAGMA page_size'))
    with pytest.raises(sqlite3.OperationalError) as refused:
        copy.include_contexts((), maximum_bytes=page.page_size, requests=(
            ContextManifestReference(selected.source, selected.record_id),
        ))
    assert refused.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    assert not ContextStore(copy.path).path.exists()
    assert not tuple(copy.path.iterdir())
    assert not copy.copied
    assert ContextStore.digest(comms.contexts.path) == before
