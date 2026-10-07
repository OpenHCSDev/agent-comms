"""Saved Pi boundary, bounded readers and presentation roundtrips."""

import json
from dataclasses import fields

import pytest

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import MessageEntry, NativeEntry, TranscriptProjection, UnknownEntry
from agent_comms.native_transcript import NativeTranscript
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


def encoded(value):
    return json.dumps(value).encode() + b"\n"


def test_native_discriminator_is_separate_from_normalized_family_codec():
    entry = NativeEntry.read(
        encoded(
            {
                "type": "message",
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
        UserTranscript("look"),
        UserTranscript("[Image attachment: image/png]"),
    ]
    assert entry.message.parts[1].data == "aGVsbG8="


def test_known_native_entries_decode_once_in_both_bounded_directions(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_bytes(
        b"".join(
            encoded(
                {
                    "type": "message",
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
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(path)))
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


def test_malformed_unknown_and_partial_entries_keep_byte_boundaries(tmp_path):
    path = tmp_path / "session.jsonl"
    extension = {"type": "plugin-note", "custom": {"future": True}}
    path.write_bytes(
        encoded(extension)
        + encoded({"type": "message", "message": {"role": "assistant", "content": 42}})
        + b'{"type":"message"\n'
        + encoded({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "valid"}]}})
        + b'{"type":"message","message":'
    )
    reader = NativeTranscript(path)
    forward = list(reader.forward(0, path.stat().st_size))
    reverse = list(reader.reverse(path.stat().st_size))
    assert forward == list(reversed(reverse))
    assert isinstance(forward[0].entry, UnknownEntry) and forward[0].entry.payload == extension
    assert forward[0].entry.events(TranscriptProjection()) == []
    assert forward[1].entry is None and forward[2].entry is None
    assert forward[3].entry.events(TranscriptProjection()) == [AssistantTranscript("valid")]
    assert forward[-1].entry is None and not forward[-1].complete
    assert forward[0].start == 0 and forward[-1].end == path.stat().st_size


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
            encoded({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": body}]}})
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
    from agent_comms.bus_publication import stable_thread_lookup
    from agent_comms.coordination_database import CoordinationStore
    from agent_comms.coordination_response import install_private_response_schema
    from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
    from agent_comms.coordinator import Coordination
    from agent_comms.transcript_receipts import AssignedTranscriptSource

    root = tmp_path / "wire"
    comms = wire(root)
    thread = comms.registry.declare(Thread("worker", frozenset(), str(tmp_path)))
    lookup = stable_thread_lookup(thread.created_at)
    database = root / "coordination.sqlite3"
    with Coordination(str(database)) as store:
        install_native_runtime_schema(store)
        install_private_response_schema(store)
        store.participants.register(lookup, thread.name, thread.name, committed=True)
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"".join(encoded(row) for row in (
        {"type": "session", "id": "source", "version": 3},
        {"type": "message", "id": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": "request"}],
            "inputId": "a" * 32, "inputDigest": "b" * 64}},
        {"type": "message", "id": "reply", "parentId": "user", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "answer"}],
            "stopReason": "stop"}},
    )))
    reader = NativeTranscript(path)
    records = tuple(reader.forward(0, path.stat().st_size))
    observe, ancestor = CoordinationStore.observing, NativeTranscript.input_ancestor
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

    def traced_ancestor(self, record):
        assert not active
        # A real zero-timeout writer commits while the native parent walk runs.
        with Coordination(str(database), lock_timeout=0) as writer:
            generation = writer.participants.get(lookup).participant_generation
            writer.participants.advance_generation(lookup, thread.name, expected_generation=generation)
        walks.append(record.entry.id)
        return ancestor(self, record)

    def traced_header(self):
        headers.append(self.path)
        return session_id(self)

    monkeypatch.setattr(CoordinationStore, "observing", traced_read)
    monkeypatch.setattr(NativeTranscript, "input_ancestor", traced_ancestor)
    monkeypatch.setattr(NativeTranscript, "session_id", property(traced_header))
    source = AssignedTranscriptSource.for_thread(root, thread, comms.bus.log)
    with comms.transcripts.routes.for_session(str(path)) as routes:
        projected = tuple(source.native_records((records,), routes, reader))
    assert walks == ["source", "reply"] and headers == [path]
    assert not active
    assert tuple(event for _, events in projected for event in events) == (
        UserTranscript("request", native_id="a" * 32), AssistantTranscript("answer"),
    )
