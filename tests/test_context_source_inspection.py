"""Source membership, public-only capture, and selected recorded values."""

from dataclasses import replace
import hashlib
import json

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_turn_context import NativeContextData, NativeContextManifestData
from agent_comms.runtime_requests import RuntimeRequest, ContextRecordedSegmentRuntimeRequest
from agent_comms.thread_identity import ThreadIncarnation, TurnId, TurnIdentity
from agent_comms.turn_context import (
    ContextManifest, ContextSourceText, FileProvenance, JournalProvenance,
    NativeProvenance, PreviewProvenance, RecordedContextTurn, SegmentManifest,
    SystemLayerSegment, TranscriptSegment, TurnContext,
)
from test_private_bus_checkpoint import _root


def system(provenance, content="Original instruction π"):
    raw = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    return SystemLayerSegment(provenance=provenance, content=content, tokens=7,
                              sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))


def identity(tmp_path):
    return NativeSessionIdentity("original-native", str(tmp_path / "session.jsonl"))


def recorded(segments):
    owner = ThreadIncarnation("original-owner", 17002.0)
    turn = RecordedContextTurn(TurnId("original-turn"), TurnIdentity(owner, 2))
    return ContextManifest(owner, turn, tuple(segments), "pi.estimateTokens", "request-one")


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
    assert data.inspection_segments == (segment,)
    assert data.observation() == original
    result = data.public_source_text(None, original, 0, source)
    assert FieldCodec.decode(ContextSourceText, FieldCodec.encode(result)) == result
    assert result.text == path.read_text()
    with pytest.raises(ValueError, match="outside"):
        data.public_source_text(None, original, 0, replace(source, path=str(tmp_path / "foreign")))
    with pytest.raises(ValueError, match="changed"):
        data.public_source_text(None, replace(original, context_digest="b" * 64), 0, source)
    path.write_text("Today's different instruction")
    with pytest.raises(ValueError, match="source has changed"):
        data.public_source_text(None, original, 0, source)


def test_borrowed_preview_refreshes_only_existing_core_contributors(tmp_path):
    comms, _ = _root(tmp_path)
    owner = replace(comms.registry.require("Alice"), session_file=str(tmp_path / "session.jsonl"))
    native = identity(tmp_path)
    segment = system((PreviewProvenance(native, "a" * 64),))
    borrowed = NativeContextData("pi.estimateTokens", native, (segment,))
    refreshed = borrowed.with_current_contributors(comms, owner)
    assert refreshed.segments is borrowed.segments
    assert refreshed.identity is borrowed.identity
    assert refreshed.observation() == borrowed.observation()
    assert refreshed.contributors == TurnContext.for_inspection(comms, owner).segments
    assert refreshed.inspection_segments == (segment, *refreshed.contributors)
    with pytest.raises(ValueError, match="different selected"):
        borrowed.with_current_contributors(comms, replace(owner, session_file=str(tmp_path / "foreign")))


def test_captured_sdk_value_is_original_and_public_only(tmp_path):
    native = identity(tmp_path)
    source = NativeProvenance(native, 3, "a" * 64)
    messages = ({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "PRIVATE_REASONING"},
        {"type": "text", "text": "Public original answer"}], "stopReason": "stop"},)
    raw = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":")).encode()
    value = TranscriptSegment(provenance=(source,), messages=messages, tokens=7,
                              sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))
    observed = NativeContextManifestData("pi.estimateTokens", (value.measured_manifest(),),
                                        request_id="request-one", values=(value,))
    original = recorded(())
    captured = observed.for_turn(original.thread, original.turn)
    assert captured.segments[0].captured_text == ("Public original answer",)
    assert "PRIVATE_REASONING" not in json.dumps(FieldCodec.encode(captured))
    with pytest.raises(ValueError, match="outside the original manifest"):
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
    request = ContextRecordedSegmentRuntimeRequest(thread="original-owner", turn=original.turn,
                request_id=original.require_request_id(), segment=0, contributors=(0,))
    assert RuntimeRequest.from_wire(request.to_wire()) == request
    selected = original.selected_segment(request.segment, request.contributors)
    assert selected is child
    borrowed = NativeContextData("pi.estimateTokens", native, (system(provenance),))
    observed = []

    async def read_reference(value):
        observed.append(value)
        return borrowed.recorded_public_text(value)

    assert await selected.public_text(read_reference) == "Original instruction π"
    assert observed == [child]
    assert child.journal_entries() == ("original-entry",)
    with pytest.raises(ValueError, match="measured bytes"):
        # System contributors annotate sources; they cannot reconstruct an
        # enclosing system value whose original bytes are unavailable.
        borrowed.recorded_public_text(root)
    with pytest.raises(ValueError, match="selected contributor"):
        original.selected_segment(0, (2,))
    with pytest.raises(ValueError, match="absent or ambiguous"):
        ContextManifest.for_request((original,), original.turn, "request-other")
    with pytest.raises(ValueError, match="not captured"):
        replace(original, request_id=None).require_request_id()


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
    from agent_comms.pi_commands import AgentCommsInspectContextSegment

    request = AgentCommsInspectContextSegment.for_manifest(root)
    assert request.parts == (value.measured_manifest(),)
    assert FieldCodec.decode(AgentCommsInspectContextSegment, FieldCodec.encode(request)) == request
    with pytest.raises(ValueError, match="measured bytes"):
        replace(acquired, segments=()).recorded_public_text(root)
    with pytest.raises(ValueError, match="outside the selected"):
        replace(acquired, segments=(replace(value, sha256="f" * 64),)).recorded_public_text(root)


def test_original_context_wire_captures_are_indexed_not_public_messages(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require("Alice")
    captured = replace(recorded((system((NativeProvenance(identity(tmp_path), 1, "a" * 64),))
                                 .measured_manifest(),)), thread=owner.incarnation)
    captured = replace(captured, segments=(replace(captured.segments[0],
                                                  captured_text=("Original request system",)),))
    comms.bus.log.record_context(captured)
    assert comms.bus.log.context_manifests(owner.name, comms.registry) == (captured,)
    assert comms.bus.log.full_history() == []
    assert comms.bus.log.latest_sequence() == 0
    assert captured.public_text_recorded
    assert not replace(captured, segments=(replace(captured.segments[0], captured_text=()),))\
        .public_text_recorded
    from agent_comms.cli_commands import ContextCliCommand

    command = ContextCliCommand(thread=owner.name, turn=2)
    assert command.encode_result(command.apply(comms))["text_recorded"] is True


def test_authored_sdk_observation_retains_read_identity_without_minting_native_proof(tmp_path):
    native = identity(tmp_path)
    preview = PreviewProvenance(native, "a" * 64)
    original = system((preview, JournalProvenance(native.session_file, ("entry",)))).measured_manifest()
    assert original.native_identity() == native
    assert original.journal_entries() == ("entry",)
    assert not any(isinstance(source, NativeProvenance) for source in original.provenance)
    with pytest.raises(ValueError, match="unambiguous"):
        replace(original, provenance=(*original.provenance,
            PreviewProvenance(replace(native, session_id="other"), "b" * 64))).native_identity()


def test_cold_client_acquires_current_and_recorded_contributor_declarations():
    """Prevent producer-import order from changing RPC and manifest decoding."""
    import subprocess
    import sys
    from agent_comms.context_segments.awareness import UnavailableAwarenessSegment
    from agent_comms.turn_context import InstructionFile

    source = FileProvenance("/original/instructions/awareness-unavailable.md", "a" * 64)
    original = "Original unavailable awareness instruction"
    contributor = UnavailableAwarenessSegment(
        provenance=(source,), instruction=InstructionFile(original, source))
    payload = FieldCodec.encode(NativeContextData(
        "pi.estimateTokens", NativeSessionIdentity("original-native", "/original/saved.jsonl"),
        (), (contributor,)))
    result = subprocess.run([sys.executable, "-c", '''
import hashlib, json, sys
from agent_comms.field_codec import FieldCodec
from agent_comms.native_turn_context import NativeContextData
from agent_comms.turn_context import ContextSegment, SegmentManifest
assert "agent_comms.context_segments.awareness" not in sys.modules
preview = FieldCodec.decode(NativeContextData, json.load(sys.stdin))
contributor = preview.contributors[0]
assert contributor.public_text() == "Original unavailable awareness instruction"
schema = FieldCodec.value_schema(type[ContextSegment])
# Original stored spellings span every previously producer-local module.
expected = {"awareness", "unavailable_awareness", "retained", "selected_wake",
            "selected_triage", "selected_work", "selected_response", "complete_awareness"}
assert expected <= set(schema["enum"]), schema
for name in expected:
    manifest = SegmentManifest(ContextSegment.decode(name), contributor.provenance,
        hashlib.sha256(contributor.public_text().encode()).hexdigest(), 40, 7)
    assert FieldCodec.decode(SegmentManifest, FieldCodec.encode(manifest)) == manifest
    assert FieldCodec.encode(manifest)["kind"] == name
try:
    ContextSegment.decode("not_an_original_contributor")
except ValueError:
    pass
else:
    raise AssertionError("Unknown context kind must still refuse")
print(json.dumps({"current_preview": "decoded original unavailable contribution", "recorded_declarations": sorted(expected), "schema_members": len(schema["enum"])}))
'''], input=json.dumps(payload), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
