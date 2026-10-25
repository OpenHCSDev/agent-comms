"""Saved Pi boundary, bounded readers and presentation roundtrips."""

import json
from dataclasses import fields

import pytest

from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.native_entries import MessageEntry, NativeEntry, TranscriptProjection, UnknownEntry
from agent_comms.native_tool_call import ToolCallId
from agent_comms.native_transcript import NativeTranscript
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
from agent_comms.turn_item import NativeItemId
from conftest import transcript_records

STAMPED = 1790812800.0  # "2026-10-01T00:00:00.000Z", the time every fixture row records


def encoded(value):
    return json.dumps(value).encode() + b"\n"


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
    # The entry was read from its own bytes, so its record starts at offset 0.
    assert entry.events(TranscriptProjection(position=0)) == [
        transcript_records.event(UserTranscript, "look", position=0),
        transcript_records.event(UserTranscript, "[Image attachment: image/png]", position=0),
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


@pytest.mark.parametrize("raw, error, match", [
    (encoded({"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "message": {"role": "assistant", "content": 42}}),
     ValueError, r"^message\.content: Expected a JSON array\.; fix: supply .*\btuple\[agent_comms\.pi_payloads\.PiContent, \.\.\.\]"),
    (b'{"type":"message"\n', MalformedRecord,
     r"^Invalid JSON: Expecting ',' delimiter: line 2 column 1 \(char 18\)$"),
])
@pytest.mark.parametrize("read", [
    lambda reader, size: reader.forward(0, size),
    lambda reader, size: reader.reverse(size),
    lambda reader, size: reader.tail(),
], ids=["forward", "reverse", "tail"])
def test_corrupt_complete_entries_raise_in_every_read_direction(tmp_path, raw, error, match, read):
    path = tmp_path / "corrupt.jsonl"
    path.write_bytes(raw)
    with pytest.raises(error, match=match):
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
    if isinstance(event, ToolStartTranscript):
        assert "text" not in payload and "ok" not in payload and "diff" not in payload
    if isinstance(event, AssistantTranscript):
        assert "tool_call_id" not in payload and "raw_input" not in payload


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


