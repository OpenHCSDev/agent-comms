"""Saved Pi boundary, bounded readers and presentation roundtrips."""

import json
from collections.abc import Callable
from dataclasses import fields, replace
from pathlib import Path
from shutil import copyfile
from typing import BinaryIO, Literal

import pytest

from agent_comms.nominal.field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    ToolCallId,
)
from agent_comms.native_entries import MessageEntry, NativeEntry, NativeEvidenceScope, TranscriptProjection, UnknownEntry
from agent_comms.native_transcript import NativeTranscript, RetainedNativeTranscript
from agent_comms.native_output import NativeRecordLocation
from agent_comms.coordination_errors import TranscriptChanged
from agent_comms.errors import RelationViolationError
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.private_path import FileRevision
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.session_file import BusSessionLocation, RetainedSessionLocation, SessionFile
from agent_comms.native_input_proof import NativeContextProof
from agent_comms.native_session_reopen import NativeSessionIdentity, NativeSessionSnapshot, PiSessionId
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
from conftest import installed_comms, transcript_records
from agent_comms.comms import wire
from agent_comms.transcripts import NativeTranscriptCursor
from agent_comms.ui_model.unread import ReplyUnread
from agent_comms.view_unread import TranscriptReadKeys
from native_unread_fixture import completed_counts

STAMPED = 1790812800.0  # "2026-10-01T00:00:00.000Z", the time every fixture row records


def encoded(value):
    return json.dumps(value).encode() + b"\n"


@pytest.fixture
def retained_native_location(tmp_path: Path) -> tuple[Path, RetainedSessionLocation]:
    session = PiSessionId.fresh()
    root = installed_comms(tmp_path / "wire").root
    original = tmp_path / "original" / "session.jsonl"
    original.parent.mkdir(mode=0o700)
    original.write_bytes(
        encoded(dict(type="session", version=3, id=session, cwd=str(tmp_path),
                     timestamp="2026-10-01T00:00:00.000Z"))
        + encoded(dict(type="message", id="original-entry", parentId=None,
                       timestamp="2026-10-01T00:00:00.000Z",
                       message=dict(role="assistant", content=[dict(type="text", text="saved")],
                                    stopReason="stop")))
    )
    original.chmod(0o600)
    with NativeEvidenceScope() as scope:
        identity = NativeSessionSnapshot.from_evidence(scope.for_source(original))
    storage = root / "retained" / "session.jsonl"
    storage.parent.mkdir()
    copyfile(original, storage)
    storage.chmod(0o600)
    revision = FileRevision.capture(storage).require(ValueError("Missing retained fixture"))
    assert revision.identity != identity.revision.identity
    original.unlink()
    location = RetainedSessionLocation(
        BusSessionLocation(storage.relative_to(root)), identity, revision,
    )
    return root, location


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
    assert runtime.tool_watch_locations() == ()
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


@pytest.mark.parametrize("read", [
    pytest.param(lambda reader, size, output: reader.cut(), id="cut"),
    pytest.param(lambda reader, size, output: reader.committed_output_cut(output), id="committed-cut"),
    pytest.param(lambda reader, size, output: reader.imported_sources(), id="imported-sources"),
    pytest.param(lambda reader, size, output: tuple(reader.tail()), id="tail"),
    pytest.param(lambda reader, size, output: tuple(reader.forward(0, size)), id="forward"),
    pytest.param(lambda reader, size, output: tuple(reader.reverse(size)), id="reverse"),
])
def test_retained_reader_rejects_descriptor_from_directory_aba(
    retained_native_location: tuple[Path, RetainedSessionLocation],
    monkeypatch: pytest.MonkeyPatch,
    read: Callable[[NativeTranscript, int, NativeRecordLocation], object],
) -> None:
    root, location = retained_native_location
    path = location.storage.resolve(root)
    reader = RetainedNativeTranscript(path, location.identity, location.revision)
    size = reader.cut().offset
    last = tuple(reader.forward(0, size))[-1]
    output = NativeRecordLocation(
        session_id=location.identity.session_id,
        session_file=location.identity.session_file,
        session_entry_id=NativeItemId("original-entry"),
        session_entry_start=last.start,
        session_entry_end=last.end,
    )
    replacement = root / "other-retained"
    replacement.mkdir()
    other = replacement / path.name
    other.write_bytes(path.read_bytes().replace(b'"saved"', b'"other"'))
    parked = root / "parked-retained"
    physical_open = Path.open
    opened: list[FileRevision] = []

    def open_after_directory_swap(candidate: Path, mode: Literal["rb"]) -> BinaryIO:
        if candidate != path:
            return physical_open(candidate, mode)
        # The foreign IO boundary opens real B, then restores A before returning
        # the descriptor. No product reader or revision method is replaced.
        path.parent.rename(parked)
        replacement.rename(path.parent)
        try:
            stream = physical_open(candidate, mode)
            opened.append(FileRevision.from_file(stream))
        finally:
            path.parent.rename(replacement)
            parked.rename(path.parent)
        return stream

    monkeypatch.setattr(Path, "open", open_after_directory_swap)
    with pytest.raises(TranscriptChanged):
        read(reader, size, output)
    assert len(opened) == 1
    assert opened[0].identity != location.revision.identity
    assert FileRevision.capture(path) == location.revision


def test_retained_cut_validates_descriptor_before_translating_original_coordinates(
    retained_native_location: tuple[Path, RetainedSessionLocation],
) -> None:
    root, location = retained_native_location
    path = location.storage.resolve(root)
    reader = RetainedNativeTranscript(path, location.identity, location.revision)
    other = root / "same-header.jsonl"
    copyfile(path, other)
    with other.open("rb") as stream, pytest.raises(TranscriptChanged):
        reader.session_cut(FileRevision.from_file(stream), location.revision.size)


@pytest.mark.parametrize("already_read", [False, True])
def test_retained_marks_and_unread_index_keep_the_original_inode(
    retained_native_location: tuple[Path, RetainedSessionLocation], already_read: bool,
) -> None:
    root, location = retained_native_location
    comms = wire(root)
    comms.registry.declare(Thread("reader", frozenset(), Checkout(root), session_file=location))
    runtime = comms.registry.require("reader").runtime
    state = comms.views.transcript_reads
    key = location.identity.session_file
    original_inode = location.identity.revision.identity.inode
    size = location.identity.revision.size
    if already_read:
        state.reads.mark_transcript("viewer", key, original_inode, size, size)
    assert completed_counts(state, "viewer").counts["reader"] == ReplyUnread(0 if already_read else 1)
    cut = runtime.read_cut()
    assert cut.revision == location.identity.revision
    NativeTranscriptCursor(key, cut.offset).mark_read(state, "viewer", runtime)
    assert state.reads.transcript_seen("viewer", key, original_inode) == cut.offset
    assert state.reads.transcript_seen("viewer", key, location.revision.identity.inode) == 0
    assert completed_counts(state, "viewer").counts["reader"] == ReplyUnread(0)
    assert runtime.read_position(size + 1, location.revision) == size


def test_original_read_key_does_not_follow_a_recreated_path_alias(
    retained_native_location: tuple[Path, RetainedSessionLocation],
) -> None:
    root, location = retained_native_location
    state = wire(root).views.transcript_reads
    runtime = location.bound_to(root).location(PiNativeBackend)
    key = runtime.read_key
    cut = runtime.read_cut()
    NativeTranscriptCursor(key, cut.offset).mark_read(state, "viewer", runtime)
    directory = location.identity.path.parent
    directory.rmdir()
    replacement = root / "different-original"
    replacement.mkdir()
    directory.symlink_to(replacement, target_is_directory=True)
    assert str(location.identity.path.resolve()) != key
    assert state.reads.transcript_seen("viewer", key, location.identity.revision.identity.inode) == cut.offset
    assert runtime.read_cut() == cut


def test_retained_unread_observation_watches_physical_storage_and_rejects_replacement(
    retained_native_location: tuple[Path, RetainedSessionLocation],
) -> None:
    root, location = retained_native_location
    comms = wire(root)
    comms.registry.declare(Thread("reader", frozenset(), Checkout(root), session_file=location))
    keys = TranscriptReadKeys.capture(comms.registry.require("reader"))
    path = location.storage.resolve(root)
    assert keys.paths == {location.identity.session_file: path}
    replacement = root / "replacement.jsonl"
    copyfile(path, replacement)
    replacement.replace(path)
    with pytest.raises(TranscriptChanged):
        keys.refresh({})


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


