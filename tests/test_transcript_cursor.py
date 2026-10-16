"""The existing saved shape selects a native or assigned-wire cursor once."""

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.transcript_sources import (
    AssignedSourceCursor,
    NativeCursor,
    TranscriptCursor,
    WireCursor,
)


def test_native_saved_position_decodes_directly_without_a_wire_field():
    saved = {"session_file": "session.jsonl", "offset": 9, "receipts": None}
    cursor = FieldCodec.decode(TranscriptCursor, saved)
    assert type(cursor) is NativeCursor
    assert FieldCodec.encode(cursor) == saved
    assert FieldCodec.decode(NativeCursor, saved) == cursor
    with pytest.raises(ValueError, match="WireCursor"):
        FieldCodec.decode(WireCursor, saved)
    assert not hasattr(cursor, "assigned_source")
    assert cursor.at_sequence(0) is cursor
    assert cursor.wire_seq == 0 and not cursor.covers_incoming(1)
    with pytest.raises(ValueError, match="cannot advance"):
        cursor.at_sequence(1)
    assert FieldCodec.decode(TranscriptCursor, {"session_file": "session.jsonl", "offset": 9}) == cursor


def test_assigned_position_keeps_the_saved_key_and_owns_its_checks():
    source = AssignedSourceCursor("/wire", RecordedThreadIncarnation("worker", 123.0), 5)
    saved = {"session_file": "session.jsonl", "offset": 9,
             "receipts": FieldCodec.encode(source)}
    cursor = FieldCodec.decode(TranscriptCursor, saved)
    assert type(cursor) is WireCursor
    assert cursor.assigned_source == source
    assert FieldCodec.decode(WireCursor, saved) == cursor
    with pytest.raises(ValueError, match="NativeCursor"):
        FieldCodec.decode(NativeCursor, saved)
    assert FieldCodec.value_schema(WireCursor)["type"] == "object"
    assert FieldCodec.encode(cursor) == saved
    assert cursor.at_sequence(3).assigned_source == source.at(3)
    assert cursor.covers_incoming(5) and not cursor.covers_incoming(6)
    assert cursor.at_offset(2).assigned_source == source
    prefix = NativeCursor("earlier.jsonl", 1)
    assert cursor.at_native_prefix(prefix) == WireCursor("earlier.jsonl", 1, source)
    assert cursor.contains(NativeCursor("session.jsonl", 2))
    assert not NativeCursor("session.jsonl", 20).contains(cursor)
    with pytest.raises(ValueError, match="cannot acquire"):
        cursor.require_native()
    with pytest.raises(ValueError, match="different transcript source"):
        cursor.require_source(AssignedSourceCursor("/other", source.recipient, 5).identity)


def test_malformed_saved_position_never_falls_back_to_native():
    with pytest.raises(ValueError):
        FieldCodec.decode(TranscriptCursor, {"session_file": "session.jsonl", "offset": 9,
                                             "receipts": "not-a-position"})
    with pytest.raises(ValueError, match="Unknown fields"):
        FieldCodec.decode(TranscriptCursor, {"session_file": "session.jsonl", "offset": 9,
                                             "unexpected": True})
    assert "anyOf" in FieldCodec.value_schema(TranscriptCursor)
