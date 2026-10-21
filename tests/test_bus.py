import os
import signal
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from bus_record_fixture import publication_processes, record_from
from agent_comms.bus import (
    Bus,
    BusRecord,
    KeyedPrivateRecord,
    PrivateRecord,
    PublicRecord,
)
from agent_comms.bus_publication import Delivered, HumanOrigin, KeyedPublication
from agent_comms.bus_tables import (
    AnsweredSource, BusProjectionTables, BusRow, BusTables, DirectReplySource, MessageIdentitySource,
)
from agent_comms.coordination_tables.publications import PublicationIntents
from agent_comms.errors import (
    HumanSendRefusedError,
    CommittedMessageError,
    RelationViolationError,
)
from agent_comms.messages import (
    ClaimHistoryKind,
    InfoKind,
    MembershipChange,
    Message,
    MessageRoleWire,
)
from agent_comms.response_acknowledgement import (
    canonical_publication_key,
)
from agent_comms.typed_table import SQLiteTableName, TypedTable


@pytest.fixture
def bus(tmp_path):
    return Bus.install(Bus.path_in(tmp_path))


def test_bus_attachment_and_failed_write_do_not_install_a_schema(tmp_path):
    bus = Bus(Bus.path_in(tmp_path / "not-installed"))
    assert not bus.path.parent.exists()
    with bus.writer_connection() as db:
        assert not SQLiteTableName.stored(db)
    with pytest.raises(sqlite3.OperationalError):
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("author", "worker", "not admitted", InfoKind, seq=seq),
        ))
    with bus.observing() as db:
        assert not SQLiteTableName.stored(db)
    installed = Bus.install(bus.path)
    record = installed.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "installed", InfoKind, seq=seq),
    ))
    assert record.seq == 1 and Bus(bus.path).lookup(1) == record


def test_install_refuses_existing_schema_changes_until_an_explicit_rebuild(bus):
    record = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "original", InfoKind, seq=seq),
    ))
    with bus.writing() as db:
        db.execute(f'DROP TABLE "{AnsweredSource.declared_name}"')
        stored = {row: row.select(db) for row in BusTables.schema_rows()}
    with pytest.raises(RelationViolationError):
        Bus.install(bus.path)
    # The actual page needs no reply index. Normal reads/writes do not inspect DDL.
    assert bus.page(2, 1) == (record,)
    second = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "second", InfoKind, seq=seq),
    ))
    with bus.writing() as db:
        assert all(row.select(db)[:len(rows)] == rows for row, rows in stored.items())
        BusProjectionTables.reset_schema(db)
    Bus.install(bus.path)
    assert bus.page(3, 2) == (second, record)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
@pytest.mark.parametrize(
    "role",
    tuple(
        MessageRoleWire(change=change, notice=notice).role()
        for change in (MessageRoleWire().change, *MembershipChange)
        for notice in (False, True)
    ),
)
def test_real_message_record_round_trip(bus, declaration, role):
    message = Message("author", "worker", "hello", InfoKind, seq=1, role=role)
    record = record_from(declaration, message)
    stored = bus.append(lambda seq, db: replace(record, seq=seq))
    assert stored.seq > 0
    assert bus.lookup(stored.seq) == stored == record
    assert Bus(bus.path).lookup(stored.seq) == stored
    assert bus.page(stored.seq + 1, 10) == (stored,)
    assert stored.message is stored
    assert stored.publication_snapshot == message.publication_snapshot
    assert type(stored.publication_snapshot) is Message
    intent = PublicationIntents(
        execution_id="e" * 32, exact_target=message.target,
        message=message.publication_snapshot,
        publication_key=canonical_publication_key("e" * 32, message.target),
        expected_message_id=message.message_id,
    )
    assert intent.matches_publication(stored)
    assert stored.messages() == (stored,)
    assert stored.deliveries() == ((stored,) if issubclass(declaration, Delivered) else ())
    for delivery in stored.deliveries():
        assert not delivery.direct_for("unaddressed")
    with pytest.raises(RelationViolationError):
        bus.lookup(stored.seq + 1)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
def test_shared_projection_reset_preserves_history_and_replays_the_record_family(bus, declaration):
    original = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "original", InfoKind, seq=seq),
    ))
    record = bus.append(lambda seq, db: record_from(
        declaration, Message("author", "worker", "kept reply", InfoKind, seq=seq,
                             answers=(original.reference,)),
    ))
    with bus.writing() as db:
        stored = {row: row.select(db) for row in BusTables.schema_rows()}
        projected = {row: row.select(db) for row in BusProjectionTables.schema_rows()}
        assert MessageIdentitySource.count(db) == 2
        BusProjectionTables.reset_schema(db)
        BusProjectionTables.require_schema(db)
        assert {row: row.select(db) for row in BusProjectionTables.schema_rows()} == projected
        assert {row: row.select(db) for row in BusTables.schema_rows()} == stored
    assert bus.lookup(record.seq) == record
    assert bus.lookup_message(record.message_id) == record
    assert bus.page(record.seq + 1, 10) == (record, original)


def test_acknowledgement_is_idempotent_without_extra_rows(bus):
    first = bus.append(
        lambda seq, db: record_from(
            KeyedPrivateRecord, Message("author", "worker", "first", InfoKind, seq=seq)
        )
    )
    with closing(sqlite3.connect(bus.path)) as db:
        before = {table: table.count(db) for table in TypedTable.members_with(BusTables)}
    assert bus.append(lambda seq, db: replace(first, seq=seq)) == first
    # Rebuilding the receipt changes its sequence-bound digest, not the publication content.
    assert bus.append(lambda seq, db: record_from(
        KeyedPrivateRecord, replace(first.publication_snapshot, seq=seq),
    )) == first
    for build in (
        lambda seq, db: replace(first, seq=seq, body="different"),
        lambda seq, db: record_from(
            KeyedPrivateRecord, replace(first.publication_snapshot, seq=seq, body="different"),
        ),
    ):
        with pytest.raises(RelationViolationError, match="different message content"):
            bus.append(build)
    with closing(sqlite3.connect(bus.path)) as db:
        assert {table: table.count(db) for table in TypedTable.members_with(BusTables)} == before
        assert db.execute("SELECT seq FROM sqlite_sequence").fetchall() == [(first.seq,)]
    assert bus.lookup(first.seq) == first
    assert bus.keyed_messages(first.acknowledgement.publication_key) == (first,)
    assert bus.keyed_messages(canonical_publication_key("f" * 32, "absent")) == ()
    with bus.reading() as snapshot:
        assert snapshot.keyed_messages(first.acknowledgement.publication_key) == (first,)


def test_publication_key_is_unique_in_sql_even_when_receipts_differ(bus):
    first = bus.append(lambda seq, db: record_from(
        KeyedPrivateRecord, Message("author", "worker", "first", InfoKind, seq=seq),
    ))
    other = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "other", InfoKind, seq=seq),
    ))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
        with bus.writing() as db:
            KeyedPublication.table.insert_values(
                db, seq=other.seq,
                acknowledgement=replace(first.acknowledgement, envelope_digest="f" * 64),
            )
    assert bus.keyed_messages(first.acknowledgement.publication_key) == (first,)


def test_every_bus_table_rejects_update_and_delete(bus):
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


def test_inbox_filters_target_and_orders_by_sequence(bus):
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


def test_scoped_and_frozen_source_queries_are_sequence_bounded(bus):
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


def test_sigkill_after_header_rolls_back_every_capability_table(bus):
    with bus.writing() as db:
        before = {table: table.count(db) for table in TypedTable.members_with(BusTables)}
    reader, writer = publication_processes.Pipe(duplex=False)
    process = publication_processes.Process(target=append_until_header, args=(bus.path, writer))
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


def test_human_refusal_rolls_back_the_header(tmp_path, bus):
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


def test_notification_failure_carries_the_committed_message(tmp_path, bus):
    bus.path.with_suffix(".changed").mkdir()
    origin = HumanOrigin("author", 1, str(tmp_path))
    with pytest.raises(CommittedMessageError) as failed:
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
        "Change notification failure: committed message returned on CommittedMessageError.record"
    )


def test_readonly_archive_and_backup_use_the_same_bus(tmp_path):
    bus = Bus(Bus.path_in(tmp_path / "source"))
    assert not bus.path.parent.exists()
    bus = Bus.install(bus.path)
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
