"""Bus history uses sequence keys; Pi session cursors remain byte positions."""

import json
from dataclasses import replace

from agent_comms.bus import Bus, PublicRecord
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import RegistryFile
from agent_comms.message_bus import MessageBus
from agent_comms.messages import InfoKind, Message
from agent_comms.native_transcript import NativeTranscript
from agent_comms.publisher import Publisher
from agent_comms.registration import Registration
from agent_comms.transcripts import NativeTranscriptCursor, TranscriptCursor


def channel_bus(root):
    messages = MessageBus(root, Registration(RegistryFile.path_in(root)), Publisher)
    for index in range(5):
        message = Message("author", "#history", f"message {index + 1}", InfoKind)
        messages.bus.append(lambda seq, db: PublicRecord.from_message(replace(message, seq=seq)))
    return messages


def pages(messages):
    before = messages.bus.revision() + 1
    result = []
    while page := messages.page(before_seq=before, limit=2):
        result.append(tuple(row.seq for row in page))
        before = page[-1].seq
    return result


def test_message_bus_pages_five_channel_messages_two_at_a_time(tmp_path):
    assert pages(channel_bus(tmp_path)) == [(5, 4), (3, 2), (1,)]


def test_archived_database_pages_read_only(tmp_path):
    live = channel_bus(tmp_path / "live")
    archive = tmp_path / "archive"
    archive.mkdir()
    live.bus.backup(Bus.path_in(archive))
    archived = MessageBus(archive, Registration(RegistryFile.path_in(archive)), Publisher)
    archived.bus.path.chmod(0o400)
    assert pages(archived) == [(5, 4), (3, 2), (1,)]
    with archived.bus.observing() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1


def test_native_cursor_resumes_by_file_byte_offset(tmp_path):
    session = tmp_path / "session.jsonl"
    records = tuple((json.dumps({
        "type": "message", "timestamp": "2026-10-01T00:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False) + "\n").encode() for text in ("first λ", "second"))
    session.write_bytes(b"".join(records))
    cursor = NativeTranscriptCursor(str(session), len(records[0]))
    restored = FieldCodec.decode(TranscriptCursor, FieldCodec.encode(cursor))
    resumed = tuple(NativeTranscript(session).forward(restored.offset, session.stat().st_size))
    assert restored == cursor
    assert len(resumed) == 1
    assert resumed[0].start == len(records[0])
    assert resumed[0].end == sum(map(len, records))
    assert [event.text for event in resumed[0].events] == ["second"]
