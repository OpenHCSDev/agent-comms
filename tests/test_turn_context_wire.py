"""Original sealed messaging survives silent context rows through every projection."""
from conftest import publish_initial
import hashlib
import json

from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.turn_context import ContextManifest, RecordedContextTurn, SegmentManifest, OwnerProvenance, TurnContext, TranscriptSegment, UserInputSegment
from agent_comms.pi_commands import Prompt
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.image_input import ImageInput
from agent_comms.field_codec import FieldCodec
from bus_record_fixture import context_comms


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


def test_awareness_inspection_reads_the_committed_source_beside_a_held_writer(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_comms.context_segments.awareness import AwarenessSegment

    comms = context_comms(tmp_path)
    publish_initial(comms, 'sender', '#team', '@Alice original question')
    owner = comms.registry.require('Alice')
    original = comms.bus.awareness_segments(owner, ())
    assert len(original) == 1 and isinstance(original[0], AwarenessSegment)
    before = comms.bus.bus.revision()
    locked, release = Event(), Event()

    def hold_writer():
        with comms.bus.bus.writing():
            locked.set()
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as worker:
        writer = worker.submit(hold_writer)
        assert locked.wait(1)
        try:
            # Another thread holds the bus write transaction; the reader keeps
            # its committed snapshot instead of waiting or omitting awareness.
            assert comms.bus.awareness_segments(owner, ()) == original
        finally:
            release.set()
        writer.result(timeout=2)
    inspection = TurnContext.for_inspection(comms, owner)
    assert original[0] in inspection.segments
    assert comms.bus.bus.revision() == before


def test_rendered_contributors_remain_original_bytes_through_prompt_boundary(tmp_path):
    comms = context_comms(tmp_path)
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


