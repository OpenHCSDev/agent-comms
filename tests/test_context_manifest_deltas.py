"""Recorded context reuses source-bound text and transcript prefixes."""
from dataclasses import replace
import hashlib

from agent_comms.field_codec import FieldCodec
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.turn_context import (
    ContextManifest, JournalProvenance, NativeProvenance, RecordedContextTurn,
    SegmentManifest, SystemLayerSegment, ToolCatalogSegment, TranscriptSegment,
)
from test_private_bus_checkpoint import _root


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
        RecordedContextTurn(TurnId(f'turn-{turn}'), TurnIdentity(owner.incarnation, turn)),
        (segment(SystemLayerSegment, 's' * 27000),
         segment(ToolCatalogSegment, 't' * 64000), transcript), 'counter', f'request-{turn}')


def test_context_deltas_restore_original_text_and_incremental_membership(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 328, 1)
    second = context(owner, tmp_path, 330, 2)
    for manifest in (first, second, replace(second, turn=replace(second.turn, identity=TurnId('turn-3')),
                                          request_id='request-3')):
        comms.bus.log.record_context(manifest)
    page = comms.bus.log.context_manifest_page(owner.name, comms.registry)
    assert len(page.resources) == 3
    decoded = [resource.pointer.decode_bytes(resource.raw, resource.source.root_id)
               for resource in page.resources]
    assert len(page.resources[1].raw) < len(page.resources[0].raw) / 10
    assert decoded[1].segments[2].inherited_entries == 328
    assert len(decoded[1].segments[2].entries) == 2
    assert decoded[2].segments[2].previous == decoded[1].segments[2].previous
    for expected, actual in zip((first, second), (r.value for r in page.resources)):
        assert actual.segments[:2] == expected.segments[:2]
        assert actual.segments[2].contributors == expected.segments[2].contributors
        assert actual.segments[2].recorded_public_text({}) == expected.segments[2].recorded_public_text({})
    # Independent readers and retained-page readers resolve the same original values.
    assert comms.bus.log.context_manifest_page(owner.name, comms.registry).resources == page.resources
    assert comms.bus.log.context_manifest_page(owner.name, comms.registry,
                                               previous=page.resources).resources == page.resources
    assert FieldCodec.decode(type(decoded[1]), FieldCodec.encode(decoded[1])) == decoded[1]


def test_changed_text_and_replaced_transcript_do_not_reuse_old_values(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    first = context(owner, tmp_path, 3, 1)
    comms.bus.log.record_context(first)
    second = context(owner, tmp_path, 1, 2)
    text = replace(second.segments[0], sha256='b' * 64, captured_text=('Changed system',))
    second = replace(second, segments=(text, *second.segments[1:]))
    comms.bus.log.record_context(second)
    page = comms.bus.log.context_manifest_page(owner.name, comms.registry)
    assert page.resources[-1].value.segments[0].captured_text == ('Changed system',)
    assert len(page.resources[-1].value.segments[2].contributors) == 1
