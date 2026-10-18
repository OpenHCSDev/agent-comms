"""Original sealed messaging survives silent context rows through every projection."""
from conftest import publish_initial
import hashlib

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.turn_context import ContextManifest, RecordedContextManifest, RecordedContextTurn, SegmentManifest, OwnerProvenance, TurnContext, TranscriptSegment, UserInputSegment
from agent_comms.pi_commands import Prompt
from agent_comms.image_inputs import ImageInput
from agent_comms.wire_record import WireRecord, AuthoredObservationWireRecord, RecordedContextManifestWireObservation
from agent_comms.cli_commands import TurnContextCliCommand, DiffContextCliCommand
from agent_comms.comms import Comms
from test_private_bus_checkpoint import _root, _page


def manifest(owner, generation=1):
    source=OwnerProvenance(owner.incarnation, 'original-input-source')
    segment=SegmentManifest(TranscriptSegment,(source,),hashlib.sha256(b'PRIVATE INPUT').hexdigest(),13,4)
    return ContextManifest(owner.incarnation,RecordedContextTurn(TurnId('original-turn'),TurnIdentity(owner.incarnation,generation)),(segment,),'pi.estimateTokens')


def test_awareness_inspection_waits_for_original_source_without_input_delay(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from agent_comms.context_segments.awareness import AwarenessSegment, UnavailableAwarenessSegment

    comms, _ = _root(tmp_path)
    publish_initial(comms, 'sender', '#team', '@Alice original question')
    owner = comms.registry.require('Alice')
    original = comms.bus.awareness_segments(owner, ())
    assert len(original) == 1 and isinstance(original[0], AwarenessSegment)
    before = comms.bus.log.path.read_bytes()
    entered = Event()

    def read():
        entered.set()
        return comms.bus.awareness_segments(owner, (), blocking=True)

    with ThreadPoolExecutor(max_workers=1) as worker:
        with comms.bus.log.locked():
            # Ordinary input still omits optional awareness immediately.
            omitted = comms.bus.awareness_segments(owner, ())
            assert len(omitted) == 1 and isinstance(omitted[0], UnavailableAwarenessSegment)
            pending = worker.submit(read)
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.05)
        assert pending.result(timeout=2) == original
    inspection = TurnContext.for_inspection(comms, owner)
    assert original[0] in inspection.segments
    assert comms.bus.log.path.read_bytes() == before


def test_silent_manifest_continuous_original_message_and_cold_projection(tmp_path, monkeypatch):
    comms,root_id=_root(tmp_path)
    first=publish_initial(comms, 'sender','#team','@Alice original question')
    lookup=RecordedThreadIncarnation("fixture", 17002.0).lookup
    prior=_page(comms,lookup)
    raw=comms.bus.log.path.read_bytes()
    with comms.bus.opened_activity() as clocks:
        activity=clocks.channels
    pending=comms.bus.pending_counts_all(['Alice','Bob'])
    awareness=comms.bus.awareness_segments(comms.registry.require('Alice'), ())
    comms.bus.log.record_context(manifest(comms.registry.require('Alice')))
    assert comms.bus.log.path.read_bytes().startswith(raw)
    assert b'PRIVATE INPUT' not in comms.bus.log.path.read_bytes()[len(raw):]
    assert comms.bus.log.latest_sequence()==first.seq
    assert len(list(comms.bus.full_history_page().messages))==1
    assert _page(comms,lookup)[0].offset>prior[0].offset
    assert _page(comms,lookup)[1:]==prior[1:]
    with comms.bus.opened_activity() as clocks:
        assert clocks.channels==activity
    assert comms.bus.pending_counts_all(['Alice','Bob'])==pending
    assert comms.bus.awareness_segments(comms.registry.require('Alice'), ())==awareness
    assert list(comms.bus.full_history_page().messages)==[first]
    with comms.bus.log.full_history_snapshot() as (_, messages):
        assert list(messages)==[first]
    second=publish_initial(comms, 'sender','#team','@Alice next original message')
    assert second.seq==first.seq+1
    comms.bus.log.record_context(manifest(comms.registry.require('Alice'),2))
    reopened=Comms(comms.root)
    assert list(reopened.bus.full_history_page().messages)==[first,second]
    assert len(_page(reopened,lookup)[1])==2
    monkeypatch.setattr(type(reopened.relationships), "RECENT_MESSAGES", 2)
    recent, limited = reopened.relationships._recent_messages()
    assert recent == (first, second) and not limited
    assert reopened.bus.pending_counts_all(['Alice','Bob'])['Alice']==2
    recorded = TurnContextCliCommand(thread='Alice',turn=1)
    assert len(recorded.encode_result(recorded.apply(reopened))['manifests'])==1
    difference = DiffContextCliCommand(thread='Alice')
    assert difference.encode_result(difference.apply(reopened))['turn']['occurrence']['generation']==2


def test_observation_family_rejects_message_fields_and_preview(tmp_path):
    comms,root_id=_root(tmp_path)
    recorded, _entries = RecordedContextManifest.capture(manifest(comms.registry.require('Alice')))
    row=AuthoredObservationWireRecord(RecordedContextManifestWireObservation(recorded))
    assert WireRecord.from_wire(row.to_wire(),root_id).messages()==()
    assert row.sequence_after(39)==39
    with pytest.raises(ValueError):
        WireRecord.from_wire({**row.to_wire(),'seq':40},root_id)


def test_rendered_contributors_remain_original_bytes_through_prompt_boundary(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    images = (ImageInput('YQ==', 'image/png'),)
    context = TurnContext.for_owner(owner, manifest(owner).turn, 'Original unicode task π 🙂', (), (), comms.goals)
    rendered = context.render(images=images)
    expected = ''.join(segment.text() for segment in context.segments)
    assert rendered.text == expected
    raw = expected.encode()
    for source in rendered.contributions:
        assert hashlib.sha256(raw[source.offset:source.offset+source.length]).hexdigest() == source.sha256
    assert sum(source.length for source in rendered.contributions) == len(raw)
    assert next(source for source in rendered.contributions if source.kind is UserInputSegment).images == (0,)
    command = Prompt(input_id='a'*32, message=rendered.text, images=images,
                     context_contributions=rendered.contributions)
    decoded = Prompt.from_wire(command.to_rpc())
    assert decoded == command
    assert decoded.message.encode() == raw and decoded.images == images


def test_current_relevance_resource_is_frozen_with_coordination_provenance(tmp_path, monkeypatch):
    from agent_comms.turn_context import CoordinationSegment, InstructionFile
    from agent_comms.wake_policy import WakePolicy

    comms, _ = _root(tmp_path)
    owner = comms.registry.require('Alice')
    original = WakePolicy.relevance_instruction()
    captured = CoordinationSegment.capture(owner, (), (), comms.goals)
    assert original.source in captured.provenance
    assert hashlib.sha256(original.content.encode()).hexdigest() == original.source.sha256

    def changed_instruction(cls, name):
        raise AssertionError('Captured instructions must not reread the current file')

    monkeypatch.setattr(InstructionFile, 'read', classmethod(changed_instruction))
    assert original.content in captured.text()
    assert captured.response_instruction.source == original.source
