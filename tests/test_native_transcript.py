"""Saved Pi boundary, bounded readers and presentation roundtrips."""

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_comms.nominal.field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    ToolCallId,
)
from agent_comms.native_entries import MessageEntry, NativeEntry, TranscriptProjection, UnknownEntry
from agent_comms.native_transcript import NativeTranscript, RetainedNativeTranscript
from agent_comms.coordination_errors import TranscriptChanged
from agent_comms.errors import RelationViolationError
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.private_path import FileRevision
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.session_file import BusSessionLocation, RetainedSessionLocation, SessionFile
from agent_comms.native_input_proof import NativeContextProof
from agent_comms.native_session_reopen import NativeSessionIdentity, PiSessionId
from agent_comms.pi_payloads import UserMessage
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    ToolEndTranscript,
    ToolStartTranscript,
    TranscriptEvent,
    UserTranscript,
)
from agent_comms.turn_item import FreshNativeId, NativeItemId
from conftest import transcript_records

STAMPED = 1790812800.0  # "2026-10-01T00:00:00.000Z", the time every fixture row records


def encoded(value):
    return json.dumps(value).encode() + b"\n"


@pytest.fixture
def retained_native_location(tmp_path: Path) -> tuple[Path, RetainedSessionLocation]:
    session = PiSessionId.fresh()
    identity = NativeSessionIdentity(session, str(tmp_path / "original" / "session.jsonl"))
    storage = tmp_path / "retained" / "session.jsonl"
    storage.parent.mkdir()
    storage.write_bytes(
        encoded(dict(type="session", version=3, id=session, cwd=str(tmp_path),
                     timestamp="2026-10-01T00:00:00.000Z"))
        + encoded(dict(type="message", id="original-entry", parentId=None,
                       timestamp="2026-10-01T00:00:00.000Z",
                       message=dict(role="assistant", content=[dict(type="text", text="saved")],
                                    stopReason="stop")))
    )
    revision = FileRevision.capture(storage).require(ValueError("Missing retained fixture"))
    location = RetainedSessionLocation(
        BusSessionLocation(storage.relative_to(tmp_path)), identity, revision,
    )
    return tmp_path, location


def test_retained_location_roundtrip_keeps_original_keys_and_reads_retained_bytes(
    retained_native_location: tuple[Path, RetainedSessionLocation],
) -> None:
    root, original = retained_native_location
    restored = FieldCodec.decode(SessionFile, FieldCodec.encode(original)).bound_to(root)
    runtime = restored.location(PiNativeBackend)
    assert not original.identity.path.exists()
    assert runtime.read_key == original.identity.session_file
    assert Path(runtime.path) == original.storage.resolve(root)
    reader = runtime.history_source(root, Thread("reader", frozenset(), Checkout(root)).incarnation)
    with reader.reading():
        cut = reader.cut()
        records = tuple(reader.forward(0, cut.offset))
    assert reader.source_path == original.identity.path
    assert reader.session_id == original.identity.session_id
    assert records[-1].require_entry().require_entry_id() == FreshNativeId("original-entry")
    assert records[-1].events[0].text == "saved"
    assert not runtime.resumable
    assert runtime.storage_files() == ()
    with pytest.raises(RelationViolationError):
        restored.writable_location(PiNativeBackend)
    with pytest.raises(NativePiUnavailable):
        runtime.require_forkable()
    with pytest.raises(NativePiUnavailable):
        runtime.arguments()
    with pytest.raises(NativePiUnavailable):
        runtime.new_location(root, root)


def test_retained_reader_rejects_another_header_identity(
    retained_native_location: tuple[Path, RetainedSessionLocation],
) -> None:
    root, original = retained_native_location
    changed = replace(original.identity, session_id=PiSessionId.fresh())
    with pytest.raises(TranscriptChanged):
        RetainedNativeTranscript(original.storage.resolve(root), changed, original.revision)


def test_retained_reader_rejects_changed_bytes_during_its_read(
    retained_native_location: tuple[Path, RetainedSessionLocation],
) -> None:
    root, original = retained_native_location
    path = original.storage.resolve(root)
    reader = RetainedNativeTranscript(path, original.identity, original.revision)
    with pytest.raises(TranscriptChanged):
        with reader.reading():
            with path.open("ab") as output:
                output.write(b"\n")
    with pytest.raises(TranscriptChanged):
        reader.cut()


def test_native_discriminator_is_separate_from_normalized_family_codec():
    entry = NativeEntry.read(
        encoded(
            {
                "type": "message", "timestamp": "2026-10-01T00:00:00.000Z",
                "id": "input",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                    ],
                },
            }
        )
    )
    assert isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage)
    normalized = FieldCodec.encode(entry)
    assert normalized["kind"] == "message" and normalized["message"]["kind"] == "user"
    assert "type" not in normalized and "role" not in normalized["message"]
    # The entry was read from its own bytes, so its record starts at offset 0; its two
    # parts share that position and are told apart by their ordinal in the record.
    assert entry.events(TranscriptProjection(position=0)) == [
        transcript_records.event(UserTranscript, "look", position=0),
        replace(transcript_records.event(UserTranscript, "[Image attachment: image/png]", position=0), ordinal=1),
    ]
    assert entry.message.parts[1].data == "aGVsbG8="


def test_unknown_and_partial_entries_keep_byte_boundaries(tmp_path):
    path = tmp_path / "session.jsonl"
    extension = {"type": "plugin-note", "timestamp": "2026-10-01T00:00:00.000Z", "custom": {"future": True}}
    path.write_bytes(
        encoded(extension)
        + encoded({"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "valid"}]}})
        + b'{"type":"message","message":'
    )
    reader = NativeTranscript(path)
    forward = list(reader.forward(0, path.stat().st_size))
    reverse = list(reader.reverse(path.stat().st_size))
    assert len(forward) == 3
    assert forward == list(reversed(reverse))
    assert isinstance(forward[0].entry, UnknownEntry) and forward[0].entry.payload == extension
    assert forward[0].events == []
    assert forward[1].events == [transcript_records.event(AssistantTranscript, "valid", position=forward[1].start)]
    assert forward[-1].entry is None and not forward[-1].complete
    assert forward[0].start == 0 and forward[-1].end == path.stat().st_size


@pytest.mark.parametrize("raw, error", [
    (encoded({"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "message": {"role": "assistant", "content": 42}}),
     ValueError),
    (b'{"type":"message"\n', MalformedRecord),
])
@pytest.mark.parametrize("read", [
    lambda reader, size: reader.forward(0, size),
    lambda reader, size: reader.reverse(size),
    lambda reader, size: reader.tail(),
], ids=["forward", "reverse", "tail"])
def test_corrupt_complete_entries_raise_in_every_read_direction(tmp_path, raw, error, read):
    path = tmp_path / "corrupt.jsonl"
    path.write_bytes(raw)
    with pytest.raises(error):
        tuple(read(NativeTranscript(path), path.stat().st_size))


@pytest.mark.parametrize(
    "event",
    [
        # Codec data: a byte offset and a provider item ID are both recorded positions.
        transcript_records.event(AssistantTranscript, "answer", position=0),
        transcript_records.event(ThinkingTranscript, "reason", position=512),
        transcript_records.event(NoticeTranscript, "compacted", position=NativeItemId("item-1")),
        transcript_records.event(ToolStartTranscript, position=1024, tool_call_id=ToolCallId("a"), tool_name="read", raw_input={"path": "x"}),
        transcript_records.event(ToolEndTranscript, position=1024, tool_call_id=ToolCallId("a"), tool_name="read", text="contents", ok=False),
    ],
)
def test_snapshot_family_roundtrip_has_only_owned_fields(event):
    payload = FieldCodec.encode(event)
    assert FieldCodec.decode(TranscriptEvent, payload) == event
    assert set(payload) <= {"kind", *(f.name for f in fields(event))}


def test_large_record_retained_in_tail_and_both_page_directions(tmp_path):
    path = tmp_path / "large.jsonl"
    bodies = ["before", "λ" * (300 * 1024), "after"]
    path.write_bytes(
        b"".join(
            encoded({"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": body}]}})
            for body in bodies
        )
    )
    transcript = NativeTranscript(path)
    size = path.stat().st_size
    forward = list(transcript.forward(0, size))
    assert list(transcript.reverse(size)) == list(reversed(forward))
    assert list(transcript.tail()) == [record.entry for record in reversed(forward)]
    assert list(transcript.tail(max_bytes=size)) == list(transcript.tail())
    # The caller-requested byte window remains a view limit, not a record quota.
    assert list(transcript.tail(max_bytes=512)) == [forward[-1].entry]


@pytest.mark.parametrize(('stop_reason', 'body', 'parent', 'next_input', 'complete', 'answered'), (
    ('stop', 'A committed final', 'opening-input', False, True, True),
    ('toolUse', 'Before a tool', 'opening-input', False, True, False),
    ('aborted', 'Interrupted output', 'opening-input', False, True, False),
    ('error', 'Failed output', 'opening-input', False, True, False),
    ('stop', '', 'opening-input', False, True, False),
    ('stop', 'Another branch', 'older-input', False, True, False),
    ('stop', 'Another turn', 'next-input', True, True, False),
    ('stop', 'An unfinished record', 'opening-input', False, False, False),
))
def test_native_final_stays_inside_its_proved_input_and_ancestry(
    tmp_path: Path, stop_reason: str, body: str, parent: str, next_input: bool, complete: bool, answered: bool,
) -> None:
    path = tmp_path / 'answer.jsonl'
    session_id = PiSessionId.fresh()
    input_id = InputId.mint()
    prefix = encoded(dict(type='session', version=3, id=session_id, cwd=str(tmp_path),
                          timestamp='2026-10-01T00:00:00.000Z'))
    # Neither the forward answer read nor its bounded ancestry walk may decode old history.
    prefix += b'{an unrelated older row that is outside this input}\n'
    opening = encoded(dict(type='message', id='opening-input', parentId='older-input',
        timestamp='2026-10-01T00:00:00.000Z', message=dict(role='user', content='The opening question',
            inputId=input_id, inputDigest='b' * 64)))
    following = encoded(dict(type='message', id='next-input', parentId='opening-input',
        timestamp='2026-10-01T00:00:00.000Z', message=dict(role='user', content='A different question')))
    final = encoded(dict(type='message', id='answer', parentId=parent,
        timestamp='2026-10-01T00:00:00.000Z', message=dict(role='assistant',
            content=[dict(type='text', text=body)], stopReason=stop_reason)))
    path.write_bytes(prefix + opening + (following if next_input else b'') +
                     (final if complete else final.removesuffix(b'\n')))
    context = NativeContextProof(input_id=input_id, session_id=session_id,
        session_entry_id=FreshNativeId('opening-input'), request_generation=1, llm_context_digest='a' * 64,
        committed_at_ms=1761699600000, session_file=path, session_entry_start=len(prefix))
    assert NativeTranscript(path).completed_input(context) is answered


