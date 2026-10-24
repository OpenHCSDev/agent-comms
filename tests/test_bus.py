import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.bus import (
    Bus,
    BusRecord,
    KeyedPrivateRecord,
    PrivateRecord,
    PublicRecord,
)
from agent_comms.bus_publication import Delivered, HumanOrigin, KeyedPublication
from agent_comms.bus_tables import AnsweredSource, BusSchema, MessageIdentitySource
from agent_comms.messages import (
    BusProjectionTables,
    BusRow,
    BusTables,
    Claimed,
    ClaimHistoryKind,
    InfoKind,
    MembershipTransition,
    Message,
    MessageRoleWire,
    NoticeRole,
)
from agent_comms.coordination_tables.publications import PublicationIntents
from agent_comms.errors import (
    CommittedMessageError,
    HumanSendRefusedError,
    RelationViolationError,
    UnclaimedError,
)
from agent_comms.field_codec import MessageId
from agent_comms.message_reference import MessageReference
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.response_acknowledgement import (
    canonical_publication_key,
)
from agent_comms.typed_table import (
    ProjectionProducer,
    RowNotFoundError,
    SQLiteTableName,
    TypedTable,
    WriterOwnedFile,
)
from bus_record_fixture import publication_processes, record_from


@pytest.fixture
def projection_source(tmp_path: Path) -> Path:
    checkout = tmp_path / "producer-source"
    source = checkout / "src" / "agent_comms"
    original = Path(__file__).resolve().parents[1] / "src" / "agent_comms"
    shutil.copytree(original, source, ignore=shutil.ignore_patterns("__pycache__", "skills"))
    (source / "skills").symlink_to(original / "skills", target_is_directory=True)
    subprocess.run(("git", "init", "-q", str(checkout)), check=True, capture_output=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Digest fixture",
            "-c",
            "user.email=digest@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-qm",
            "Source fixture",
        ),
        check=True,
        capture_output=True,
    )
    return source


def projection_source_digest(source: Path) -> str:
    return subprocess.run(
        (
            sys.executable,
            "-P",
            "-m",
            "agent_comms.producer_stamp",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(source.parent)},
    ).stdout.strip()


@pytest.mark.parametrize(
    ("module", "old", "new"),
    (
        (
            "typed_table.py",
            "Durable files compare their stored DDL text",
            "An unrelated infrastructure docstring: durable files compare their stored DDL text",
        ),
        (
            "field_codec.py",
            "class TimestampText(",
            "def unrelated_projection_probe():\n    return 17\n\n\nclass TimestampText(",
        ),
    ),
)
def test_projection_digest_ignores_infrastructure_source(
    projection_source: Path,
    module: str,
    old: str,
    new: str,
) -> None:
    before = projection_source_digest(projection_source)
    path = projection_source / module
    original = path.read_text()
    assert original.count(old) == 1
    path.write_text(original.replace(old, new))
    assert projection_source_digest(projection_source) == before


def test_projection_digest_tracks_declared_column_spelling(projection_source: Path) -> None:
    before = json.loads(projection_source_digest(projection_source))["message_identity_source"]
    codec = projection_source / "field_codec.py"
    original = codec.read_text()
    related = "digits: ClassVar[int] = 12"
    assert original.count(related) == 1
    codec.write_text(original.replace(related, "digits: ClassVar[int] = 13"))
    assert (
        json.loads(projection_source_digest(projection_source))["message_identity_source"] != before
    )
    unrelated = 'spelling = re.compile("[0-9a-f]{7,40}")'
    assert original.count(unrelated) == 1
    codec.write_text(original.replace(unrelated, 'spelling = re.compile("[0-9a-f]{8,40}")'))
    assert (
        json.loads(projection_source_digest(projection_source))["message_identity_source"] == before
    )


def test_nominal_message_id_column_stores_plain_text() -> None:
    identifier = MessageId.mint("nominal column storage")
    row = MessageIdentitySource(seq=1, message_id=identifier, sender="sender")
    with sqlite3.connect(":memory:") as db:
        MessageIdentitySource.create(db)
        row.insert(db)
        value, storage = db.execute(
            f'SELECT message_id, typeof(message_id) FROM "{MessageIdentitySource.declared_name}"',
        ).fetchone()
        assert storage == "text"
        assert value == str(identifier)
        restored, = MessageIdentitySource.select(db)
        assert type(restored.message_id) is MessageId
        assert restored == row


@pytest.mark.parametrize(
    ("module", "old", "new"),
    (
        (
            "bus_publication.py",
            "yield SentSource(self.audience.sender_lookup, self.seq)",
            "yield SentSource(self.audience.sender_lookup, self.seq + 1)",
        ),
        ("bus_activity_index.py", '=max("{name}",excluded.', '=min("{name}",excluded.'),
        (
            "messages.py",
            "for record in BusCapability.history(db, kinds):",
            "for record in reversed(tuple(BusCapability.history(db, kinds))):",
        ),
        (
            "messages.py",
            "yield MessageIdentitySource(self.seq, self.message_id, self.sender)",
            "yield MessageIdentitySource(self.seq, self.message_id, self.target)",
        ),
        (
            "messages.py",
            "holders.apply(db, self.claim_transition, projected)",
            "holders.apply(db, self.claim_transition, projected)\n        del projected",
        ),
        (
            "messages.py",
            "return (SentActivity(message.sender, message.timestamp),)",
            "return (SentActivity(message.target, message.timestamp),)",
        ),
        (
            "messages.py",
            "return (BusRouteCounts.from_message(message),)",
            "return (BusRouteCounts.from_message(message), BusRouteCounts.from_message(message))",
        ),
        (
            "channel_targets.py",
            "    def direct_names(self) -> tuple[str, ...]:\n        return self.names\n",
            "    def direct_names(self) -> tuple[str, ...]:\n        return self.names[:1]\n",
        ),
        (
            "activity_clocks.py",
            "message.sender_role.last_user_input(0.0, message.timestamp),",
            "message.sender_role.last_user_input(1.0, message.timestamp),",
        ),
    ),
)
def test_projection_digest_tracks_producing_source(
    projection_source: Path,
    module: str,
    old: str,
    new: str,
) -> None:
    before = projection_source_digest(projection_source)
    path = projection_source / module
    original = path.read_text()
    assert original.count(old) == 1
    path.write_text(original.replace(old, new))
    assert projection_source_digest(projection_source) != before


def test_activity_source_changes_only_sent_activity(projection_source: Path) -> None:
    before = json.loads(projection_source_digest(projection_source))
    path = projection_source / "messages.py"
    original = path.read_text()
    old = "return (SentActivity(message.sender, message.timestamp),)"
    assert original.count(old) == 1
    path.write_text(original.replace(
        old, "return (SentActivity(message.sender, message.timestamp + 1.0),)"))
    after = json.loads(projection_source_digest(projection_source))
    assert {name for name in before if before[name] != after[name]} == {"sent_activity"}


def test_display_import_does_not_add_projection_producers(projection_source: Path) -> None:
    before = projection_source_digest(projection_source)
    path = projection_source / "producer_stamp.py"
    original = path.read_text()
    declaration = "from agent_comms.bus import Bus as Bus"
    assert original.count(declaration) == 1
    path.write_text(original.replace(declaration, declaration + "\nfrom agent_comms import cli"))
    assert projection_source_digest(projection_source) == before



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


def test_projection_freshness_follows_its_producing_source(bus):
    record = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "preserved source", InfoKind, seq=seq),
    ))
    current = MessageIdentitySource.producer_digest()
    older = "0" * 64
    assert older != current
    with bus.writer_connection() as db:
        before = db.total_changes
        assert not BusSchema.schema_differs(db)
        assert not BusSchema.reconcile_schema(db, WriterOwnedFile())
        assert db.total_changes == before

        ProjectionProducer.update(
            db, where="{projection}=?", parameters=(MessageIdentitySource.declared_name,),
            producer_digest=older,
        )
        MessageIdentitySource.update(db, where="{seq}=?", parameters=(record.seq,), sender="stale")
        assert BusSchema.schema_differs(db)
        assert BusSchema.reconcile_schema(db, WriterOwnedFile())
        assert MessageIdentitySource.require(db, seq=record.seq).sender == record.sender
        assert ProjectionProducer.select(db) == list(ProjectionProducer.records_for(
            BusProjectionTables.projection_rows()))

        ProjectionProducer.drop(db)
        MessageIdentitySource.update(db, where="{seq}=?", parameters=(record.seq,), sender="stale")
        assert BusSchema.schema_differs(db)
        assert BusSchema.reconcile_schema(db, WriterOwnedFile())
        assert MessageIdentitySource.require(db, seq=record.seq).sender == record.sender
        assert ProjectionProducer.select(db) == list(ProjectionProducer.records_for(
            BusProjectionTables.projection_rows()))
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert bus.lookup(record.seq) == record


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
@pytest.mark.parametrize(
    "role",
    tuple(
        MessageRoleWire(change=change, notice=notice).role()
        for change in (MessageRoleWire().change, *MembershipTransition.members_with(MembershipTransition))
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
    assert tuple(bus.message_history()) == (Message.from_message(stored),)
    assert stored.deliveries() == ((stored,) if issubclass(declaration, Delivered) else ())
    for delivery in stored.deliveries():
        assert not delivery.direct_for("unaddressed")
    with pytest.raises(RelationViolationError):
        bus.lookup(stored.seq + 1)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
def test_record_family_rebuild_and_public_history_install_preserve_scope(bus, declaration, tmp_path):
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
    scope = RepliesDisplayScope(frozenset({"author"}), frozenset({"worker"}))
    copied = Bus.install(
        Bus.path_in(tmp_path / "selected-history"), history=scope.history(bus, limit=2),
    )
    public = copied.lookup(record.seq)
    assert type(public) is PublicRecord
    assert public.publication_snapshot == record.history_snapshot.publication_snapshot
    assert copied.lookup_message(record.message_id) == public
    assert copied.received("b" * 32) == ()
    assert copied.page(record.seq + 1, 10) == (public, copied.lookup(original.seq))
    assert copied.replies((original.reference,)) == (public,)


@pytest.mark.parametrize("declaration", BusRecord.members_with(Claimed))
def test_claim_queries_decode_the_declared_capability_in_live_and_held_reads(bus, declaration):
    plain = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "public", InfoKind, seq=seq),
    ))
    stored = bus.append(lambda seq, db: record_from(
        declaration, Message("author", "worker", "claimed", InfoKind, seq=seq),
    ))
    expected = stored.require_claim_transition()
    assert bus.claim(stored.seq) == expected
    with bus.reading() as snapshot:
        assert snapshot.claim(stored.seq) == expected
        for sequence in (plain.seq, stored.seq + 1):
            with pytest.raises(UnclaimedError) as missing:
                snapshot.claim(sequence)
            assert missing.value.seq == sequence


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
            with pytest.raises(sqlite3.DatabaseError, match="Rows cannot be updated"):
                db.execute(f'UPDATE "{table.declared_name}" SET seq=seq')
            with pytest.raises(sqlite3.DatabaseError, match="Rows cannot be deleted"):
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
    assert bus.direct_replies("b" * 32, after=original.seq, limit=1, descending=False,
                              sender_lookups=("a" * 32,)) == (reply,)
    assert bus.received("b" * 32, sender_lookups=("f" * 32,)) == ()
    assert bus.latest_addressed(frozenset({"author", "old-author"}),
                                frozenset({"worker", "old-worker"})) == (reply,)
    renamed = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("old-author", "old-worker", "newest across aliases", InfoKind, seq=seq)))
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("worker", "author", "later reverse reply", InfoKind, seq=seq)))
    assert bus.latest_addressed(frozenset({"author", "old-author"}),
                                frozenset({"worker", "old-worker"})) == (renamed,)
    assert bus.latest_addressed(frozenset({"absent", "old-absent"}),
                                frozenset({"worker", "old-worker"})) == ()
    assert bus.replies((original.reference,)) == (reply,)
    assert bus.deliveries_for_references((reply.reference,)) == (reply,)
    tips = bus.latest_conversations("target=?", ("worker",), notice.seq)
    assert tuple((row.seq, row.sender, row.target) for row in tips) == (
        (notice.seq, "author", "worker"),
    )
    selected = bus.selected_sequences("target=?", ("worker",), reply.seq)
    assert {row.seq for row in selected} == {notice.seq, reply.seq}
    assert bus.conversation("a" * 32, limit=1, descending=False) == (notice,)
    assert bus.conversation(
        "a" * 32, limit=1, descending=False, sequences=(reply.seq,),
    ) == (reply,)
    after = bus.revision()
    for index in range(101):
        bus.append(lambda seq, db, index=index: record_from(PrivateRecord, Message(
            "author", "worker", f"notice {index}", InfoKind, seq=seq, role=NoticeRole(),
        )))
    accepted = bus.append(lambda seq, db: record_from(PrivateRecord, Message(
        "author", "worker", "first eligible reply after notices", InfoKind, seq=seq,
    )))
    assert bus.direct_replies(
        "b" * 32, after=after, limit=1, descending=False, sender_lookups=("a" * 32,),
    ) == (accepted,)


@pytest.mark.parametrize("offset", (0, 100))
def test_unanswered_reference_selection_requires_the_recorded_original_and_exact_owner_answer(bus, offset):
    original = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("owner", "lead", "Recorded directive", InfoKind, seq=seq)))
    with pytest.raises(RelationViolationError):
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("lead", "#comms", "Wrong original reference", InfoKind, seq=seq,
                    answers=(MessageReference(
                        original.seq + offset, MessageId.mint("wrong-hash")),))))
    assert bus.revision() == original.seq
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("another-unit", "#comms", "Someone else's answer", InfoKind, seq=seq,
                answers=(original.reference,))))
    assert bus.unanswered_references((original.seq,), frozenset({"lead"})) == (original.reference,)
    with bus.reading() as before_reply:
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("renamed-lead", "#comms", "The owner's answer", InfoKind, seq=seq,
                    answers=(original.reference,))))
        assert before_reply.unanswered_references((original.seq,), frozenset({"lead", "renamed-lead"})) == (
            original.reference,)
    assert bus.unanswered_references((original.seq,), frozenset({"lead", "renamed-lead"})) == ()
    with pytest.raises(RowNotFoundError):
        bus.unanswered_references((original.seq, bus.revision() + 1), frozenset({"lead"}))


def test_answer_projection_rebuild_preserves_sequence_links_and_public_references(bus):
    original = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("owner", "lead", "Recorded question", InfoKind, seq=seq)))
    reply = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("lead", "owner", "Recorded answer", InfoKind, seq=seq,
                answers=(original.reference,))))
    with bus.writing() as db:
        BusProjectionTables.reset_schema(db)
    Bus.install(bus.path)
    with bus.observing() as db:
        assert AnsweredSource.select(db, original=original.seq) == [
            AnsweredSource(original.seq, reply.seq),
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert bus.lookup(reply.seq).answers == (original.reference,)
    assert bus.replies((original.reference,)) == (reply,)
    assert bus.unanswered_references((original.seq,), frozenset({"lead"})) == ()


def test_bulk_history_keeps_one_snapshot_across_pages(bus):
    assert tuple(bus.message_history()) == ()
    originals = tuple(Message.from_message(bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", f"message {seq}", InfoKind, seq=seq),
    ))) for _ in range(101))
    history = bus.message_history()
    assert next(history) == originals[0]
    later = Message.from_message(bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "after export started", InfoKind, seq=seq),
    )))
    assert tuple(history) == originals[1:]
    assert tuple(bus.message_history()) == (*originals, later)


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
    with pytest.raises(HumanSendRefusedError, match="was not sent") as failed:
        origin.append(
            bus,
            lambda seq, db: record_from(
                PrivateRecord, Message("author", "worker", "refused", InfoKind, seq=seq)
            ),
        )
    assert isinstance(failed.value.cause, sqlite3.IntegrityError)
    assert failed.value.__cause__ is failed.value.cause
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


def test_human_programming_failure_propagates_after_rollback(tmp_path, bus):
    failure = RuntimeError("Broken publication implementation")

    def build(seq, db):
        record_from(
            PrivateRecord, Message("author", "worker", "not committed", InfoKind, seq=seq)
        ).append_to(db, seq)
        raise failure

    with pytest.raises(RuntimeError) as failed:
        HumanOrigin("author", 1, str(tmp_path)).append(bus, build)
    assert failed.value is failure
    with closing(sqlite3.connect(bus.path)) as db:
        assert all(table.count(db) == 0 for table in TypedTable.members_with(BusTables))


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
