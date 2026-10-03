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
    data = FieldCodec.decode(NativeContextData, {
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
        borrowed.recorded_public_text(root)
    with pytest.raises(ValueError, match="selected contributor"):
        original.selected_segment(0, (2,))
    with pytest.raises(ValueError, match="absent or ambiguous"):
        ContextManifest.for_request((original,), original.turn, "request-other")
    with pytest.raises(ValueError, match="not captured"):
        replace(original, request_id=None).require_request_id()


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
