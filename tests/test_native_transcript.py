"""Saved Pi boundary, bounded readers and presentation roundtrips."""

import json
from dataclasses import fields
from unittest.mock import Mock

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import MessageEntry, NativeEntry, TranscriptProjection, UnknownEntry
from agent_comms.coordination_errors import TranscriptChanged
from agent_comms.native_transcript import MissingSessionCut, NativeTranscript, PresentSessionCut
from agent_comms.pi_payloads import UserMessage
from agent_comms.threads import Thread
from agent_comms.transcript_events import (
    AssistantTranscript,
    NoticeTranscript,
    ThinkingTranscript,
    ToolEndTranscript,
    ToolStartTranscript,
    TranscriptEvent,
    UserTranscript,
)


def test_missing_session_has_nothing_to_mark_read(tmp_path):
    cut = NativeTranscript(tmp_path / "missing.jsonl").cut()
    reads = Mock()
    assert isinstance(cut, MissingSessionCut) and cut.offset == 0
    cut.mark_read(reads, "viewer", "missing.jsonl", 0)
    reads.mark_transcript.assert_not_called()


def test_present_session_rejects_a_read_past_its_cut(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_bytes(b"x\n")
    cut = NativeTranscript(source).cut()
    reads = Mock()
    assert isinstance(cut, PresentSessionCut)
    cut.mark_read(reads, "viewer", str(source), 2)
    reads.mark_transcript.assert_called_once_with(
        "viewer", str(source), source.stat().st_ino, 2, 2)
    with pytest.raises(TranscriptChanged):
        cut.mark_read(reads, "viewer", str(source), 3)


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
    assert entry.events(TranscriptProjection()) == [
        UserTranscript("look", timestamp=STAMPED),
        UserTranscript("[Image attachment: image/png]", timestamp=STAMPED),
    ]
    assert entry.message.parts[1].data == "aGVsbG8="


def test_known_native_entries_decode_once_in_both_bounded_directions(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_bytes(
        b"".join(
            encoded(
                {
                    "type": "message", "timestamp": "2026-10-01T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(i)}],
                    },
                }
            )
            for i in range(60)
        )
    )
    comms = wire(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path), session_file=str(path)))
    decoded = []
    original = NativeEntry.read

    def decode(raw):
        decoded.append(raw)
        return original(raw)

    monkeypatch.setattr(NativeEntry, "read", decode)
    latest = comms.transcripts.thread_transcript_page("worker", max_messages=5)
    assert len(decoded) == len(set(decoded)) == 6
    assert [e.text for e in latest.events] == [str(i) for i in range(55, 60)]
    decoded.clear()
    older = comms.transcripts.thread_transcript_page("worker", before=latest.before, max_messages=5)
    assert len(decoded) == len(set(decoded)) == 6
    decoded.clear()
    forward = comms.transcripts.thread_transcript_page(
        "worker", after=older.after, through=latest.after, max_messages=5
    )
    assert len(decoded) == 5 and forward.events == latest.events


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
    assert forward[0].entry.events(TranscriptProjection()) == []
    assert forward[1].entry.events(TranscriptProjection()) == [AssistantTranscript("valid", timestamp=STAMPED)]
    assert forward[-1].entry is None and not forward[-1].complete
    assert forward[0].start == 0 and forward[-1].end == path.stat().st_size


@pytest.mark.parametrize("raw, error, match", [
    (encoded({"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "message": {"role": "assistant", "content": 42}}),
     ValueError, r"^Expected a JSON array\.$"),
    (b'{"type":"message"\n', json.JSONDecodeError,
     r"^Expecting ',' delimiter: line 2 column 1 \(char 18\)$"),
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
        AssistantTranscript("answer"),
        ThinkingTranscript("reason"),
        NoticeTranscript("compacted"),
        ToolStartTranscript(tool_call_id="a", tool_name="read", raw_input={"path": "x"}),
        ToolEndTranscript(tool_call_id="a", tool_name="read", text="contents", ok=False),
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


def test_native_ancestry_is_acquired_before_coordinator_read(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agent_comms.coordination_database import CoordinationStore, StoreBusy
    from agent_comms.coordination_response import install_private_response_schema
    from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
    from agent_comms.coordinator import Coordination
    from agent_comms.transcript_sources import LiveAssignedTranscriptSource

    root = tmp_path / "wire"
    comms = wire(root)
    thread = comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path)))
    lookup = RecordedThreadIncarnation("fixture", thread.created_at).lookup
    database = root / "coordination.sqlite3"
    with Coordination(str(database)) as store:
        install_native_runtime_schema(store)
        install_private_response_schema(store)
        store.participants.register(lookup, thread.name, thread.name, committed=True)
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"".join(encoded(row) for row in (
        {"type": "session", "timestamp": "2026-10-01T00:00:00.000Z", "id": "source", "version": 3},
        {"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "id": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": "request"}],
            "inputId": "a" * 32, "inputDigest": "b" * 64}},
        {"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "id": "reply", "parentId": "user", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "answer"}],
            "stopReason": "stop"}},
    )))
    reader = NativeTranscript(path)
    records = tuple(reader.forward(0, path.stat().st_size))
    observe, ancestry = CoordinationStore.observing, NativeTranscript.inputs_for
    session_id = NativeTranscript.session_id.fget
    active, headers, walks = [], [], []

    @contextmanager
    def traced_read(*args, **kwargs):
        with observe(*args, **kwargs) as db:
            active.append(db)
            try:
                yield db
            finally:
                active.pop()

    def traced_ancestry(self, records):
        assert not active
        # A fail-fast writer commits while the native parent walk runs.
        with Coordination(str(database), contention=StoreBusy()) as writer:
            generation = writer.participants.get(lookup).participant_generation
            writer.participants.advance_generation(lookup, thread.name, expected_generation=generation)
        walks.append(tuple(record.entry.id for record in records))
        return ancestry(self, records)

    def traced_header(self):
        headers.append(self.path)
        return session_id(self)

    monkeypatch.setattr(CoordinationStore, "observing", traced_read)
    monkeypatch.setattr(NativeTranscript, "inputs_for", traced_ancestry)
    monkeypatch.setattr(NativeTranscript, "session_id", property(traced_header))
    source = LiveAssignedTranscriptSource.for_thread(root, thread, comms.bus.log)
    with comms.transcripts.routes.for_session(str(path)) as routes:
        projected = tuple(source.native_records((records,), routes, reader))
    # One parent walk serves the whole fragment.
    assert walks == [tuple(record.entry.id for record in records)] and headers == [path]
    assert not active
    assert tuple(event for _, events in projected for event in events) == (
        UserTranscript("request", timestamp=STAMPED, native_id="a" * 32),
        AssistantTranscript("answer", timestamp=STAMPED),
    )
