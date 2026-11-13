"""Source membership, public-only capture, and selected recorded values."""
import hashlib
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms.cli_verbs.turn_context import TurnContextCliCommand
from agent_comms.context_segments.awareness import AwarenessSegment
from agent_comms.context_segments.native_messages import TranscriptSegment
from agent_comms.execution.context_prior import UNREAD_CONTEXT
from agent_comms.execution.native_session_reopen import NativeSessionIdentity, PiSessionId
from agent_comms.execution.native_turn_context import NativeContextManifestData
from agent_comms.execution.pi_commands import AgentCommsInspectContextSegment
from agent_comms.execution.runtime import RuntimeConnection
from agent_comms.execution.session_file import NoSessionFile
from agent_comms.execution.turn_context import (
    ContextManifest,
    ContextSourceText,
    ContributionCoordinates,
    FileProvenance,
    InstructionFile,
    JournalProvenance,
    NativeProvenance,
    NextContextTurn,
    ObservedDispatchRequest,
    PreviewProvenance,
    RecordedContextTurn,
    SystemLayerSegment,
    TurnContext,
    UnattributedProvenance,
    UnobservedDispatchRequest,
    UnretainedSourceText,
    UserInputSegment,
)
from agent_comms.execution.turn_item import PiRequestKey
from agent_comms.nominal.field_codec import FieldCodec, TurnId
from agent_comms.pi_payloads import NativeContextData
from agent_comms.registry.thread_identity import (
    RecordedThreadIncarnation,
    ReportedTools,
    TurnIdentity,
)
from agent_comms.transport.owner_connection import socket_path
from agent_comms.transport.runtime_request import RuntimeRequest
from agent_comms.transport.runtime_requests import (
    ContextCoreRuntimeRequest,
    ContextCoreSourceRuntimeRequest,
    ContextRecordedSegmentRuntimeRequest,
)
from bus_history import read_history
from bus_record_fixture import context_comms

# noqa: F401
from delivery_owner_fixture import (
    canonical_delivery_owner,
    qualified_owner_launch,
    start_headless_owner,
)


def system(provenance, content="Original instruction π"):
    raw = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    return SystemLayerSegment(provenance=provenance, content=content, tokens=7,
                              sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))


def identity(tmp_path):
    return NativeSessionIdentity(PiSessionId("original-native"), str(tmp_path / "session.jsonl"))


def recorded(segments):
    owner = RecordedThreadIncarnation("original-owner", 17002.0)
    turn = RecordedContextTurn(
        TurnId('ab87f599dc0a50a56b7c65de71c39426'), TurnIdentity(owner, 2)
    )
    return ContextManifest(thread=owner, turn=turn, segments=tuple(segments), counter="pi.estimateTokens",
                           request_id=ObservedDispatchRequest(PiRequestKey("request-one")))


def test_current_native89_shape_and_authenticated_file_read(tmp_path):
    native = identity(tmp_path)
    original = PreviewProvenance(native, "a" * 64)
    path = tmp_path / "instruction.md"
    path.write_text("Original instruction π")
    source = FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest())
    segment = system((original, source))
    # Native89 has no Core contributors or captured-value field. The existing
    # external decoder still accepts that precise current-preview projection.
    data = NativeContextData.from_wire({
        "counter": "pi.estimateTokens", "identity": FieldCodec.encode(native),
        "segments": FieldCodec.encode((segment,)),
    })
    assert data.segments == (segment,)
    assert data.observation() == original
    selected = data.public_source(original, 0, source)
    assert selected == source
    result = ContextSourceText(selected.public_description(), selected.public_text(None))
    assert FieldCodec.decode(ContextSourceText, FieldCodec.encode(result)) == result
    assert result.text == path.read_text()
    with pytest.raises(ValueError):
        data.public_source(original, 0, replace(source, path=str(tmp_path / "foreign")))
    with pytest.raises(ValueError):
        data.public_source(replace(original, context_digest="b" * 64), 0, source)
    path.write_text("Today's different instruction")
    with pytest.raises(ValueError):
        data.public_source(original, 0, source).public_text(None)


def test_current_core_context_is_independent_and_source_selection_is_current(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require("Alice")
    context = comms.context_for(owner)
    assert FieldCodec.decode(TurnContext, FieldCodec.encode(context)) == context
    request = ContextCoreRuntimeRequest(thread=owner.name)
    assert FieldCodec.decode(RuntimeRequest, FieldCodec.encode(request)) == request
    position, segment = next((i, s) for i, s in enumerate(context.segments)
                             if any(isinstance(p, FileProvenance) for p in s.provenance))
    source = next(p for p in segment.provenance if isinstance(p, FileProvenance))
    selected = ContextCoreSourceRuntimeRequest(thread=owner.name, owner=owner.incarnation,
        segment=position, manifest=segment.manifest(0), source=source)
    assert FieldCodec.decode(RuntimeRequest, FieldCodec.encode(selected)) == selected
    text = context.public_source_text(comms, selected.owner, selected.segment,
                                      selected.manifest, selected.source)
    assert text.text == source.public_text(comms)
    with pytest.raises(ValueError):
        context.public_source_text(comms, replace(owner.incarnation, created_at=owner.created_at + 1),
            position, selected.manifest, source)
    with pytest.raises(ValueError):
        context.public_source_text(comms, owner.incarnation, position,
                                  replace(selected.manifest, sha256="b" * 64), source)
    with pytest.raises(ValueError):
        context.public_source_text(comms, owner.incarnation, position, selected.manifest,
                                  replace(source, path="/foreign"))


@pytest.mark.asyncio
async def test_core_context_socket_reads_without_native_session_or_preview(
        tmp_path, qualified_owner_launch):

    async with canonical_delivery_owner(tmp_path) as (comms, agent, _, _):
        owner = comms.registry.require("alpha")
        assert owner.session_file == NoSessionFile()
        # An owner answers live reads only for a session under its own lease (#3982). A
        # headless owner holds that lease without any native Pi session file.
        await start_headless_owner(agent, "alpha", qualified_owner_launch)
        owner = comms.registry.require("alpha")
        connection = RuntimeConnection(comms, owner.incarnation,
                                       socket_path(comms.root, owner.require_process().pid))
        try:
            context = FieldCodec.decode(TurnContext, await connection.request("context_core"))
            assert context.thread == owner.incarnation
            assert context.segments == comms.context_for(owner).segments
            position, segment = next((i, s) for i, s in enumerate(context.segments)
                                     if any(isinstance(p, FileProvenance) for p in s.provenance))
            source = next(p for p in segment.provenance if isinstance(p, FileProvenance))
            parameters = dict(owner=owner.incarnation, segment=position,
                              manifest=segment.manifest(0), source=source)
            text = FieldCodec.decode(ContextSourceText,
                await connection.request("context_core_source", **parameters))
            assert text.text == source.public_text(comms)
            assert agent.turns.persistent_backends == {}
            assert owner.session_file == NoSessionFile()
        finally:
            await connection.close()


def test_captured_sdk_value_is_original_and_public_only(tmp_path):
    native = identity(tmp_path)
    source = NativeProvenance(native, 3, "a" * 64)
    messages = ({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "PRIVATE_REASONING"},
        {"type": "text", "text": "Public original answer"}], "stopReason": "stop"},)
    raw = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":")).encode()
    value = TranscriptSegment(provenance=(source,), messages=messages, tokens=7,
                              sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))
    observed = NativeContextManifestData(
        "pi.estimateTokens",
        (value.measured_manifest(),),
        offered_tools=ReportedTools("fixture-pi", frozenset()),
        request_id=ObservedDispatchRequest(PiRequestKey("request-one")),
        values=(value,),
    )
    original = recorded(())
    captured = observed.for_turn(original.thread, original.turn)
    assert captured.segments[0].captured_text == ("Public original answer",)
    assert "PRIVATE_REASONING" not in json.dumps(FieldCodec.encode(captured))
    with pytest.raises(ValueError):
        replace(observed, values=(replace(value, utf8_bytes=value.utf8_bytes + 1),)).for_turn(
            original.thread, original.turn)
    # Missing historical values stay absent, never today's system preview.
    old = replace(observed, values=()).for_turn(original.thread, original.turn)
    assert old.segments[0].captured_text == ()
    assert FieldCodec.decode(ContextManifest, FieldCodec.encode(old)) == old


@pytest.mark.asyncio
async def test_recorded_child_reads_exact_value_not_root_or_other_request(tmp_path):
    native = identity(tmp_path)
    provenance = (NativeProvenance(native, 1, "a" * 64),
                  JournalProvenance(native.session_file, ("original-entry",)))
    child = system(provenance).measured_manifest()
    sibling = replace(child, sha256="b" * 64, captured_text=("Sibling text",))
    root = replace(child, sha256="c" * 64, contributors=(child, sibling))
    original = recorded((root,))
    comms = context_comms(tmp_path)
    owner = comms.registry.require('Alice')
    original = replace(original, thread=owner.incarnation,
        turn=replace(original.turn, occurrence=replace(original.turn.occurrence,
                                                       incarnation=owner.incarnation)))
    comms.contexts.record_after(UNREAD_CONTEXT, original)
    page = comms.contexts.page(owner.name, comms.registry)
    request = ContextRecordedSegmentRuntimeRequest(thread=owner.name, turn=original.turn,
                request_id=original.request_id, record=page.reference(page.resources[0].value),
                segment=0, contributors=(0,))
    assert FieldCodec.decode(RuntimeRequest, FieldCodec.encode(request)) == request
    selected = original.selected_segment(request.segment, request.contributors)
    assert selected is child
    borrowed = NativeContextData("pi.estimateTokens", native, (system(provenance),))
    observed = []

    async def read_reference(value):
        observed.append(value)
        return borrowed.recorded_public_text(value)

    assert await selected.public_text(read_reference) == "Original instruction π"
    assert observed == [child]
    assert child.sources.journal_entries() == ("original-entry",)
    with pytest.raises(ValueError):
        # System contributors annotate sources; they cannot reconstruct an
        # enclosing system value whose original bytes are unavailable.
        borrowed.recorded_public_text(root)
    with pytest.raises(ValueError):
        original.selected_segment(0, (2,))
    with pytest.raises(ValueError):
        ContextManifest.for_request(
            (original,), original.turn, ObservedDispatchRequest(PiRequestKey("request-other")))
    # Pi omits requestId when it never observed dispatch, and that capture is recorded as it is:
    # it is selected by its own member, and its segments are read like any other's.
    unobserved = replace(original, request_id=UnobservedDispatchRequest())
    assert ContextManifest.for_request(
        (unobserved,), unobserved.turn, UnobservedDispatchRequest()) is unobserved
    with pytest.raises(ValueError):
        ContextManifest.for_request((unobserved,), unobserved.turn, original.request_id)
    unobserved_read = replace(request, request_id=UnobservedDispatchRequest())
    assert FieldCodec.decode(RuntimeRequest, FieldCodec.encode(unobserved_read)) == unobserved_read


@pytest.mark.asyncio
async def test_complete_recorded_root_uses_one_acquired_result(tmp_path):
    native = identity(tmp_path)
    provenance = (NativeProvenance(native, 1, "a" * 64),
                  JournalProvenance(native.session_file, ("original-entry",)))
    messages = tuple({"role": "user", "content": f"Original message {n}"} for n in range(1000))
    raw = json.dumps(list(messages), separators=(",", ":")).encode()
    value = TranscriptSegment(provenance=provenance, messages=messages, tokens=1000,
                              sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))
    parts = []
    for message in messages:
        encoded = json.dumps([message], separators=(",", ":")).encode()
        parts.append(replace(value, messages=(message,), tokens=1,
                             sha256=hashlib.sha256(encoded).hexdigest(),
                             utf8_bytes=len(encoded)).measured_manifest())
    root = replace(value, contributors=tuple(parts)).measured_manifest()
    acquired = NativeContextData("pi.estimateTokens", native, (value,))
    reads = []

    async def read_reference(expected):
        reads.append(expected)
        return acquired.recorded_public_text(expected)

    assert await root.public_text(read_reference) == "\n".join(m["content"] for m in messages)
    assert reads == [root]
    captured = replace(root, contributors=tuple(replace(p, captured_text=(f"Captured {n}",))
                                                for n, p in enumerate(parts)))
    assert captured.public_text_recorded
    reads.clear()
    assert await captured.public_text(read_reference) == "\n".join(f"Captured {n}" for n in range(1000))
    assert reads == []
    # A message's logical instruction-range annotation is not its entire body.
    annotated = replace(parts[0], contributors=(replace(system(provenance).measured_manifest(),
                                                       captured_text=("Only a range",)),))
    assert not annotated.public_text_recorded
    assert annotated.requested_parts() == ()


@pytest.mark.asyncio
async def test_mixed_recorded_root_merges_capture_and_verified_parts_once(tmp_path):
    native = identity(tmp_path)
    provenance = (NativeProvenance(native, 1, "a" * 64),
                  JournalProvenance(native.session_file, ("original-entry",)))
    message = {"role": "user", "content": "Original journal message"}
    raw = json.dumps([message], separators=(",", ":")).encode()
    value = TranscriptSegment(provenance=provenance, messages=(message,), tokens=1,
                              sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))
    captured = replace(value.measured_manifest(), sha256="b" * 64,
                       captured_text=("Original transformed message",))
    root = replace(value.measured_manifest(), sha256="c" * 64,
                   contributors=(captured, value.measured_manifest()))
    acquired = NativeContextData("pi.estimateTokens", native, (value,))
    reads = []

    async def read_reference(expected):
        reads.append(expected)
        return acquired.recorded_public_text(expected)

    assert await root.public_text(read_reference) == (
        "Original transformed message\nOriginal journal message")
    assert reads == [root]

    request = AgentCommsInspectContextSegment.for_manifest(root)
    assert request.parts == (value.measured_manifest(),)
    assert FieldCodec.decode(AgentCommsInspectContextSegment, FieldCodec.encode(request)) == request
    with pytest.raises(ValueError):
        replace(acquired, segments=()).recorded_public_text(root)
    with pytest.raises(ValueError):
        replace(acquired, segments=(replace(value, sha256="f" * 64),)).recorded_public_text(root)


def test_original_context_wire_captures_are_indexed_not_public_messages(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require("Alice")
    captured = replace(recorded((system((NativeProvenance(identity(tmp_path), 1, "a" * 64),))
                                 .measured_manifest(),)), thread=owner.incarnation)
    captured = replace(captured, segments=(replace(captured.segments[0],
                                                  captured_text=("Original request system",)),))
    comms.contexts.record_after(UNREAD_CONTEXT, captured)
    page = comms.contexts.page(owner.name, comms.registry)
    assert tuple(resource.value for resource in page.resources) == (captured,)
    assert read_history(comms) == []
    assert comms.bus.bus.revision() == 0
    assert captured.public_text_recorded
    assert not replace(captured, segments=(replace(captured.segments[0], captured_text=()),))\
        .public_text_recorded

    command = TurnContextCliCommand(thread=owner.name, turn=2)
    assert command.apply(comms).to_wire()["text_recorded"] is True


def test_authored_sdk_observation_retains_read_identity_without_minting_native_proof(tmp_path):
    native = identity(tmp_path)
    preview = PreviewProvenance(native, "a" * 64)
    original = system((preview, JournalProvenance(native.session_file, ("entry",)))).measured_manifest()
    assert original.sources.native_identity() == native
    assert original.sources.journal_entries() == ("entry",)
    assert not any(isinstance(source, NativeProvenance) for source in original.provenance)
    with pytest.raises(ValueError):
        replace(original, provenance=(*original.provenance,
            PreviewProvenance(replace(native, session_id="other"), "b" * 64))).sources.native_identity()


def test_cold_client_acquires_current_and_recorded_contributor_declarations():
    """Prevent producer-import order from changing RPC and manifest decoding."""

    source = FileProvenance("/original/instructions/awareness.md", "a" * 64)
    original = "Original awareness instruction"
    contributor = AwarenessSegment(
        provenance=(source,), instruction=InstructionFile(original, source), bus="bus", sources=())
    payload = FieldCodec.encode(TurnContext(RecordedThreadIncarnation("original-owner", 17002.0),
                                         NextContextTurn(), (contributor,)))
    result = subprocess.run([sys.executable, "-c", '''
import hashlib, json, sys
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.execution.turn_context import ContextSegment, SegmentManifest, TurnContext
assert "agent_comms.context_segments.awareness" not in sys.modules
preview = FieldCodec.decode(TurnContext, json.load(sys.stdin))
contributor = preview.segments[0]
assert contributor.public_text() == "Original awareness instruction"
schema = FieldCodec.value_schema(type[ContextSegment])
# Original stored spellings span every previously producer-local module.
expected = {"awareness", "retained", "selected_wake",
            "selected_triage", "selected_work", "selected_response", "complete_awareness"}
assert expected <= set(schema["enum"]), schema
for name in expected:
    manifest = SegmentManifest(ContextSegment.member_named(name), contributor.provenance,
        hashlib.sha256(contributor.public_text().encode()).hexdigest(), 40, 7)
    assert FieldCodec.decode(SegmentManifest, FieldCodec.encode(manifest)) == manifest
    assert FieldCodec.encode(manifest)["kind"] == name
try:
    ContextSegment.member_named("not_an_original_contributor")
except ValueError:
    pass
else:
    raise AssertionError("Unknown context kind must still refuse")
'''], input=json.dumps(payload), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_a_saved_segment_shows_its_retained_text_or_says_none_was_kept():
    """A saved record has no live owner to read from. Before, a segment that kept no public text
    came back as the not-retained notice in the field that means "the original text", and a span
    read against it raised "Contribution differs from its original addressed bytes", so opening
    an annotation on a stopped thread's record crashed the detail (toad#1557's census)."""
    segment = UserInputSegment(content="Keep the original instruction.",
                               provenance=(UnattributedProvenance(),))
    coordinates = ContributionCoordinates.capture(
        UserInputSegment, segment.provenance, 0, "Keep")
    retained = replace(segment.manifest(0), captured_text=(segment.content,)).saved_source_text()
    assert type(retained) is ContextSourceText
    assert retained.text == segment.content and retained.span_text(coordinates) == "Keep"
    unretained = segment.manifest(0).saved_source_text()
    assert isinstance(unretained, UnretainedSourceText)
    assert unretained.span_text(coordinates) == unretained.text
    with pytest.raises(ValueError):
        ContextSourceText(unretained.description, unretained.text).span_text(coordinates)
