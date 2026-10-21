"""Bus history uses sequence keys; Pi session cursors remain byte positions."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from bus_record_fixture import record_from
from agent_comms.bus import Bus, BusSnapshot, PrivateRecord, PublicRecord
from agent_comms.bus_publication import AgentOrigin, HumanOrigin
from agent_comms.bus_tables import BusRow, MessageIdentitySource
from agent_comms.errors import HumanSendRefusedError
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.locked_store import RegistryFile
from agent_comms.historical_views import HistoryArchive
from agent_comms.message_bus import MessageBus
from agent_comms.message_page import MessagePageRequest, PageBounds
from agent_comms.page_traversal import NewerTraversal, OlderTraversal
from agent_comms.read_basis import ChannelDisplayScope
from agent_comms.messaging import Messaging
from agent_comms.messages import InfoKind, Message
from agent_comms.native_transcript import NativeTranscript
from agent_comms.publisher import Publisher
from agent_comms.presentation import WireRevision
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.revision_views import BusSourceView, StoresView
from agent_comms.registration import Registration
from agent_comms.transcripts import NativeTranscriptCursor, TranscriptCursor
from agent_comms.typed_table import RowNotFoundError


def channel_bus(root):
    messages = MessageBus(root, Registration(RegistryFile.path_in(root)), Publisher)
    for index in range(5):
        message = Message("author", "#history", f"message {index + 1}", InfoKind)
        messages.bus.append(lambda seq, db: PublicRecord.from_message(replace(message, seq=seq)))
    return messages


def pages(messages):
    result = []
    with messages.bus.reading() as reader:
        before = reader.revision() + 1
        while page := reader.page(before, 2):
            result.append(tuple(row.seq for row in page))
            before = page[-1].seq
    return result


def test_message_bus_pages_five_channel_messages_two_at_a_time(tmp_path):
    assert pages(channel_bus(tmp_path)) == [(5, 4), (3, 2), (1,)]


@pytest.mark.parametrize("traversal", [OlderTraversal(), OlderTraversal(8), NewerTraversal(3)])
def test_scoped_page_rows_and_edges_share_the_admitted_cut(tmp_path, traversal):
    messages = channel_bus(tmp_path)
    request = MessagePageRequest(
        ChannelDisplayScope("#history", frozenset({"#history"})),
        PageBounds(traversal, limit=2),
    )
    with messages.bus.reading() as source:
        before = request.read_opened(source)
        messages.bus.append(lambda seq, db: PublicRecord.from_message(
            Message("author", "#history", "later", InfoKind, seq=seq)))
        retained = request.read_opened(source)
        assert retained == before
        assert tuple(row.seq for row in retained.messages) == (4, 5)
        assert retained.has_older and not retained.has_newer
    latest = request.read_opened(messages.bus)
    assert latest != retained


def test_archived_database_pages_read_only(tmp_path):
    live = channel_bus(tmp_path / "live")
    registry = Registration(RegistryFile.path_in(live.bus.path.parent))
    registry.declare(registry.new_thread("author", frozenset({"history"}), str(tmp_path)))
    source = HistoryArchive(Bus.path_in(tmp_path / "reader")).attach(live.bus.path.parent)
    archive = Path(source.root)
    archived = MessageBus(archive, Registration(RegistryFile.path_in(archive)), Publisher)
    archived.bus.path.chmod(0o400)
    archive.chmod(0o500)
    try:
        source.validate()
        assert source.snapshot_bus_revision == 5
        assert pages(archived) == [(5, 4), (3, 2), (1,)]
        with archived.bus.observing() as db:
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        archive.chmod(0o700)
    replacement = tmp_path / "replacement.sqlite3"
    live.bus.backup(replacement)
    replacement.replace(archived.bus.path)
    with pytest.raises(ValueError, match="Historical snapshot changed"):
        source.validate()


def test_message_identity_lookup_preserves_missing_and_ambiguous_refusal(tmp_path):
    messages = channel_bus(tmp_path / "source")
    original = messages.bus.lookup(1)
    assert messages.bus.lookup_message(original.message_id).reference == original.reference
    with pytest.raises(RowNotFoundError):
        messages.bus.lookup_message("missing")
    messages.bus.append(lambda seq, db: replace(original, seq=seq))
    with pytest.raises(MalformedRecord, match="Expected one"):
        messages.bus.lookup_message(original.message_id)


def test_origins_check_message_identity_inside_actual_writer_transaction(tmp_path):
    messages = channel_bus(tmp_path / "source")
    origin = HumanOrigin("author", 1, str(tmp_path))
    message = Message("author", "#history", "one human send", InfoKind)
    with messages.bus.writing() as db:
        source = BusSnapshot(db)
        origin.require_source(source, message.sender, message.message_id)
        seq = BusRow.next_seq(db)
        PublicRecord.from_message(replace(message, seq=seq)).append_to(db, seq)
        with pytest.raises(HumanSendRefusedError):
            origin.require_source(source, message.sender, message.message_id)
        AgentOrigin().require_source(source, message.sender, message.message_id)
        assert MessageIdentitySource.exists(db, where="{seq}=?", parameters=(seq,))
        assert not MessageIdentitySource.exists(
            db, where="{seq}>?", parameters=(seq,), sender=message.sender)
        assert not MessageIdentitySource.exists(db, sender="absent")
    assert messages.bus.revision() == 6
    assert messages.bus.lookup(6).message_id == message.message_id


def test_user_identity_is_created_once_under_the_registry_owner(tmp_path):
    messages = channel_bus(tmp_path / "source")
    root = messages.bus.path.parent
    clients = tuple(Messaging(root, Registration(RegistryFile.path_in(root)), messages)
                    for _ in range(2))
    with ThreadPoolExecutor(max_workers=2) as executor:
        users = tuple(executor.map(lambda client: client.user_identity(str(root)), clients))
    assert users[0].incarnation == users[1].incarnation
    registered = tuple(clients[0].registry.user_thread().existing())
    assert len(registered) == 1 and registered[0].incarnation == users[0].incarnation


def test_reading_scope_reuses_one_read_only_snapshot_and_closes_it(tmp_path):
    messages = channel_bus(tmp_path / "source")
    with messages.bus.reading() as reader:
        with reader.observing() as first, reader.observing() as second:
            assert first is second and first.execute("PRAGMA query_only").fetchone()[0] == 1
        original = reader.lookup(1)
        assert reader.lookup_message(original.message_id).reference == original.reference
        messages.bus.append(lambda seq, db: PublicRecord.from_message(
            Message("author", "#history", "later", InfoKind, seq=seq)))
        assert reader.revision() == 5 and messages.bus.revision() == 6
        assert tuple(row.seq for row in reader.page(7, 2)) == (5, 4)
    with pytest.raises(sqlite3.ProgrammingError):
        reader.lookup(1)


def test_reply_existence_uses_exact_reference_and_frozen_author(tmp_path):
    messages = channel_bus(tmp_path)
    original = messages.bus.lookup(1).reference
    other = messages.bus.lookup(2).reference
    messages.bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "#history", "unattested answer", InfoKind,
                seq=seq, answers=(original,))))
    assert not messages.bus.has_reply(original, sender_lookup="a" * 32)
    with messages.bus.reading() as source:
        assert not source.has_reply(original, sender_lookup="a" * 32)
        messages.bus.append(lambda seq, db: record_from(
            PrivateRecord, Message("renamed-author", "worker", "frozen answer", InfoKind,
                                   seq=seq, answers=(original,))))
        assert not source.has_reply(original, sender_lookup="a" * 32)
    assert messages.bus.has_reply(original, sender_lookup="a" * 32)
    assert not messages.bus.has_reply(original, sender_lookup="b" * 32)
    assert not messages.bus.has_reply(other, sender_lookup="a" * 32)
    changed_identity = replace(original, message_id="0" * len(original.message_id))
    assert not messages.bus.has_reply(changed_identity, sender_lookup="a" * 32)


def test_view_revision_distinguishes_sequence_growth_from_source_replacement(tmp_path):
    messages = channel_bus(tmp_path / "source")
    before = replace(WireRevision.empty(), bus_seq=messages.bus.revision(),
                     bus_identity=RecordedBusIdentity.capture(messages.bus.path))
    grown = replace(before, bus_seq=before.bus_seq + 1)
    assert grown.changed_since(StoresView(), before)
    assert not grown.changed_since(BusSourceView(), before)
    replacement = Bus.path_in(tmp_path / "replacement")
    messages.bus.backup(replacement)
    replaced = replace(before, bus_identity=RecordedBusIdentity.capture(replacement))
    assert replaced.changed_since(BusSourceView(), before)


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
