"""Original sealed messaging survives silent context rows through every projection."""
from conftest import publish_initial
import hashlib
import json

import pytest

from agent_comms.context_store import AbsentContext
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.turn_context import ContextManifest, RecordedContextTurn, SegmentManifest, OwnerProvenance, TurnContext, TranscriptSegment, UserInputSegment
from agent_comms.pi_commands import Prompt
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.image_inputs import ImageInput
from agent_comms.wire_record import WireRecord
from agent_comms.field_codec import FieldCodec
from test_private_bus_checkpoint import _root


def manifest(owner, generation=1):
    source=OwnerProvenance(owner.incarnation, 'original-input-source')
    segment=SegmentManifest(TranscriptSegment,(source,),hashlib.sha256(b'PRIVATE INPUT').hexdigest(),13,4)
    return ContextManifest(owner.incarnation,RecordedContextTurn(TurnId('original-turn'),TurnIdentity(owner.incarnation,generation)),(segment,),'pi.estimateTokens')


def corrupt_context_kind(comms, name, kind):
    """Damage only one owner's stored payload, not its schema or message bus."""
    from agent_comms.context_store import ContextRequest, ContextSegmentVersion

    selected = comms.contexts.page(name, comms.registry).resources[-1]
    with comms.contexts.writing() as db:
        request = ContextRequest.one(db, id=selected.record_id)
        assert request is not None
        version = ContextSegmentVersion.one(db, id=request.versions[0])
        assert version is not None
        encoded = FieldCodec.encode(version.segment)
        encoded['manifest']['kind'] = kind
        db.execute(f'UPDATE {ContextSegmentVersion.declared_name} SET segment=? WHERE id=?',
                   (json.dumps(encoded), version.id))


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

    locked, release = Event(), Event()

    def hold_writer():
        with comms.bus.log.locked():
            locked.set()
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=2) as worker:
        writer = worker.submit(hold_writer)
        assert locked.wait(1)
        try:
            # A different thread owns the writer: this is real contention,
            # unlike reentering this thread's own source lease.
            omitted = comms.bus.awareness_segments(owner, ())
            assert len(omitted) == 1 and isinstance(omitted[0], UnavailableAwarenessSegment)
            pending = worker.submit(read)
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.05)
        finally:
            release.set()
        writer.result(timeout=2)
        assert pending.result(timeout=2) == original
    inspection = TurnContext.for_inspection(comms, owner)
    assert original[0] in inspection.segments
    assert comms.bus.log.path.read_bytes() == before


def test_observation_family_rejects_message_fields_and_preview(tmp_path):
    comms,root_id=_root(tmp_path)
    recorded, _entries = AbsentContext().capture(manifest(comms.registry.require('Alice')))
    row = {'observation': {'kind': 'recorded_context_manifest',
                           'manifest': FieldCodec.encode(recorded)}}
    # Only the one-time conversion accepts the old context envelope. Runtime
    # message readers never silently accept a second storage location.
    with pytest.raises(ValueError):
        WireRecord.from_wire(row, root_id)
    with pytest.raises(ValueError):
        WireRecord.from_wire({**row, 'seq': 40}, root_id)


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
                     context_contributions=rendered.contributions,
                     owner_tools=OwnerToolConnection('/unused', 'a' * 64))
    decoded = Prompt.from_wire(command.to_rpc())
    assert decoded == command
    assert decoded.message.encode() == raw and decoded.images == images


