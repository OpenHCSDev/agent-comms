import multiprocessing
import os
import signal
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import (
    Bus,
    BusRecord,
    KeyedPrivateRecord,
    PrivateRecord,
    PublicRecord,
)
from agent_comms.bus_publication import Delivered, HumanOrigin
from agent_comms.bus_tables import BusRow, BusTables, DirectReplySource, KeyedPublication
from agent_comms.envelope_claim_transitions import ClaimRelease, ReleasedClaims
from agent_comms.errors import (
    HumanSendRefusedError,
    MessageNotificationError,
    RelationViolationError,
)
from agent_comms.messages import ClaimHistoryKind, ClaimMessage, InfoKind, Message
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement,
    canonical_publication_key,
    public_envelope_digest,
)
from agent_comms.typed_table import TypedTable
from agent_comms.wake import ControlClassification, resolve_wake_cohort


def record_from(declaration, message):
    capabilities = {}
    if issubclass(declaration, Delivered):
        audience = freeze_audience(
            message,
            (FrozenRecipient("b" * 32, message.target),),
            "c" * 64,
            sender_lookup="a" * 32,
            sender_name=message.sender,
        )
        capabilities.update(
            audience=audience,
            decisions=resolve_wake_cohort(
                message, frozen_audience=audience, control=ControlClassification.ORDINARY
            ),
            control=ControlClassification.ORDINARY.value,
        )
    if issubclass(declaration, KeyedPublication):
        capabilities["acknowledgement"] = KeyedResponseAcknowledgement(
            "d" * 32,
            "e" * 32,
            canonical_publication_key("e" * 32, message.target),
            public_envelope_digest(message.to_wire()),
        )
    if issubclass(declaration, ClaimMessage):
        return declaration.of(
            message,
            ReleasedClaims(
                owner=message.sender,
                incarnation="1",
                seq=message.seq,
                message_id=message.message_id,
                releases=(ClaimRelease("/owned/resource", "f" * 32),),
            ),
        )
    return declaration.from_message(message, **capabilities)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
def test_real_message_record_round_trip(tmp_path, declaration):
    bus = Bus(Bus.path_in(tmp_path))
    message = Message("author", "worker", "hello", InfoKind, seq=1)
    record = record_from(declaration, message)
    stored = bus.append(lambda seq, db: replace(record, seq=seq))
    assert stored.seq > 0
    assert bus.lookup(stored.seq) == stored == record
    assert Bus(bus.path).lookup(stored.seq) == stored
    assert bus.page(stored.seq + 1, 10) == (stored,)
    assert stored.message is stored
    assert stored.messages() == (stored,)
    assert stored.deliveries() == ((stored,) if issubclass(declaration, Delivered) else ())
    for delivery in stored.deliveries():
        assert not delivery.direct_for("unaddressed")
    with pytest.raises(RelationViolationError):
        bus.lookup(stored.seq + 1)


def test_acknowledgement_is_idempotent_without_extra_rows(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    first = bus.append(
        lambda seq, db: record_from(
            KeyedPrivateRecord, Message("author", "worker", "first", InfoKind, seq=seq)
        )
    )
    with closing(sqlite3.connect(bus.path)) as db:
        before = {table: table.count(db) for table in TypedTable.members_with(BusTables)}
    assert bus.append(lambda seq, db: replace(first, seq=seq, body="different")) == first
    with closing(sqlite3.connect(bus.path)) as db:
        assert {table: table.count(db) for table in TypedTable.members_with(BusTables)} == before
        assert db.execute("SELECT seq FROM sqlite_sequence").fetchall() == [(first.seq,)]
    assert bus.lookup(first.seq) == first


def test_every_bus_table_rejects_update_and_delete(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    stored = tuple(
        bus.append(
            lambda seq, db: record_from(
                declaration, Message("author", "worker", "kept", InfoKind, seq=seq)
            )
        )
        for declaration in BusRecord.members_with(BusRecord)
    )
    with closing(sqlite3.connect(bus.path)) as db:
        for table in TypedTable.members_with(BusTables):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f'UPDATE "{table.declared_name}" SET seq=seq')
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f'DELETE FROM "{table.declared_name}"')
    assert all(bus.lookup(record.seq) == record for record in stored)


def test_inbox_filters_target_and_orders_by_sequence(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    first = bus.append(
        lambda seq, db: PublicRecord.from_message(Message("author", "worker", "one", InfoKind, seq=seq))
    )
    other = bus.append(
        lambda seq, db: PublicRecord.from_message(Message("author", "other", "two", InfoKind, seq=seq))
    )
    last = bus.append(
        lambda seq, db: record_from(
            PrivateRecord, Message("author", "worker", "three", InfoKind, seq=seq)
        )
    )
    assert first.seq < last.seq
    assert bus.inbox("worker", 0, 10) == (first, last)
    assert bus.inbox("worker", first.seq, 10) == (last,)
    assert bus.inbox("worker", 0, 1) == (first,)
    assert bus.inbox("other", 0, 10) == (other,)
    assert bus.inbox("absent", 0, 10) == ()
    assert bus.page(last.seq + 1, 2) == (last, other)


def test_scoped_and_frozen_source_queries_are_sequence_bounded(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    original = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "#other", "original", InfoKind, seq=seq)))
    notice = bus.append(lambda seq, db: record_from(PrivateRecord,
        Message("author", "worker", "notice", ClaimHistoryKind, seq=seq)))
    reply = bus.append(lambda seq, db: record_from(PrivateRecord,
        Message("author", "worker", "reply", InfoKind, seq=seq, answers=(original.reference,))))
    assert bus.scoped_page("target=?", ("worker",), descending=True, limit=1) == (reply,)
    assert bus.scoped_page("target=?", ("worker",), upper=reply.seq, limit=1) == (notice,)
    assert bus.scoped_page("target=?", ("worker",), kinds=(InfoKind,), limit=2) == (reply,)
    assert bus.conversation("b" * 32, limit=2) == (reply, notice)
    assert bus.sent("a" * 32, limit=1) == (reply,)
    assert bus.received("b" * 32, after=original.seq, limit=1, descending=False,
                        sender_lookups=("a" * 32,), source=DirectReplySource) == (reply,)
    assert bus.received("b" * 32, sender_lookups=("f" * 32,)) == ()
    assert bus.replies((original.reference,)) == (reply,)
    assert bus.deliveries_for_references((reply.reference,)) == (reply,)


def append_until_header(path, ready):
    def header_inserted():
        ready.send("header inserted")
        os.kill(os.getpid(), signal.SIGSTOP)
        return 0

    class PausedBus(Bus):
        @contextmanager
        def writing(self):
            with super().writing() as db:
                db.create_function("header_inserted", 0, header_inserted)
                db.execute(
                    f'CREATE TEMP TRIGGER pause_header AFTER INSERT ON "{BusRow.declared_name}" '
                    "BEGIN SELECT header_inserted(); END"
                )
                yield db

    bus = PausedBus(path)
    bus.append(
        lambda seq, db: PublicRecord.from_message(
            Message("author", "worker", "interrupted", InfoKind, seq=seq)
        )
    )


def test_sigkill_after_header_rolls_back_every_capability_table(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    with bus.writing() as db:
        before = {table: table.count(db) for table in TypedTable.members_with(BusTables)}
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=append_until_header, args=(bus.path, writer))
    process.start()
    writer.close()
    try:
        assert reader.poll(15), "publisher never reached its inserted header"
        assert reader.recv() == "header inserted"
        process.kill()
        process.join(5)
        assert process.exitcode == -signal.SIGKILL
        with closing(sqlite3.connect(bus.path)) as db:
            assert {
                table: table.count(db) for table in TypedTable.members_with(BusTables)
            } == before
        stored = bus.append(
            lambda seq, db: PublicRecord.from_message(
                Message("author", "worker", "after crash", InfoKind, seq=seq)
            )
        )
        assert stored.seq == 1
        print("SIGKILL after header: every BusTables count unchanged; next committed seq=1")
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        reader.close()


def test_human_refusal_rolls_back_the_header(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    with bus.writing() as db:
        db.execute(
            f'CREATE TRIGGER refuse_message BEFORE INSERT ON "{Message.table.declared_name}" '
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
    origin = HumanOrigin("author", 1, str(tmp_path))
    with pytest.raises(HumanSendRefusedError, match="was not sent"):
        origin.append(
            bus,
            lambda seq, db: record_from(
                PrivateRecord, Message("author", "worker", "refused", InfoKind, seq=seq)
            ),
        )
    with closing(sqlite3.connect(bus.path)) as db:
        assert all(table.count(db) == 0 for table in TypedTable.members_with(BusTables))


def test_notification_failure_carries_the_committed_message(tmp_path):
    bus = Bus(Bus.path_in(tmp_path))
    bus.path.with_suffix(".changed").mkdir()
    origin = HumanOrigin("author", 1, str(tmp_path))
    with pytest.raises(MessageNotificationError) as failed:
        origin.append(
            bus,
            lambda seq, db: record_from(
                PrivateRecord, Message("author", "worker", "sent", InfoKind, seq=seq)
            ),
        )
    assert failed.value.record.seq == 1
    assert bus.lookup(1) == failed.value.record
    assert failed.value.record.body == "sent"
    print(
        "Change notification failure: committed message returned on MessageNotificationError.record"
    )


def test_readonly_archive_and_backup_use_the_same_bus(tmp_path):
    bus = Bus(Bus.path_in(tmp_path / "source"))
    assert not bus.path.parent.exists()
    bus.path.parent.mkdir()
    with bus.writing():
        pass
    assert bus.revision() == 0
    committed = bus.append(
        lambda seq, db: PublicRecord.from_message(
            Message("author", "worker", "archived", InfoKind, seq=seq)
        )
    )
    assert bus.revision() == committed.seq
    destination = Bus.path_in(tmp_path / "archive")
    bus.backup(destination)
    destination.chmod(0o400)
    before = destination.read_bytes()
    archived = Bus(destination)
    assert archived.revision() == committed.seq
    assert archived.lookup(committed.seq) == committed
    with archived.observing() as db, pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute(f'INSERT INTO "{BusRow.declared_name}" DEFAULT VALUES')
    assert destination.read_bytes() == before
    with pytest.raises(FileExistsError):
        bus.backup(destination)
