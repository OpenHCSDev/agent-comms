import ast
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import fixture_git
from agent_comms.coordination.response_acknowledgement import canonical_publication_key
from agent_comms.coordination.tables.publications import PublicationIntents
from agent_comms.errors import (
    CommittedMessageError,
    HumanSendRefusedError,
    RelationViolationError,
    UnclaimedError,
)
from agent_comms.messaging.bus import (
    Bus,
    BusRecord,
    KeyedPrivateRecord,
    PrivateRecord,
    PublicRecord,
)
from agent_comms.messaging.bus_publication import Delivered, KeyedPublication
from agent_comms.messaging.bus_route_counts import BusRouteCounts
from agent_comms.messaging.bus_tables import (
    AnsweredSource,
    AuthoredAnswers,
    BusSchema,
    MessageIdentitySource,
)
from agent_comms.messaging.initial_origin import HumanOrigin
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import (
    BusProjectionTables,
    BusRow,
    BusTables,
    Claimed,
    ClaimHistoryKind,
    ClaimNoticeKind,
    ConversationActivity,
    HandoffKind,
    InfoKind,
    MembershipTransition,
    Message,
    MessageKind,
    MessageRoleWire,
    MessageRoutes,
    NoticeRole,
    TeamUpdatePosition,
)
from agent_comms.messaging.read_basis import RepliesDisplayScope
from agent_comms.nominal.field_codec import GitCommitText, MessageId, MessageIdText
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.typed_table import (
    ProducedTable,
    ProjectionProducer,
    RowNotFoundError,
    SQLiteTableName,
    TypedTable,
    WriterOwnedFile,
)
from agent_comms.task_attachments.team_pings import TeamUpdate, TeamUpdateNotice
from bus_record_fixture import publication_processes, record_from


@dataclass(frozen=True)
class PristineProjection:
    """One committed copy of the source and the digest it stamps before any edit."""

    checkout: Path
    digest: str


@pytest.fixture(scope="module")
def pristine_projection(tmp_path_factory: pytest.TempPathFactory) -> PristineProjection:
    checkout = tmp_path_factory.mktemp("projection") / "producer-source"
    original = Path(__file__).resolve().parents[1] / "src"
    # Only Python declarations feed the digest; link the large non-Python trees and the
    # hooks package the source imports.
    linked = ("skills", "node_modules")
    source = checkout / "src" / "agent_comms"
    shutil.copytree(original / "agent_comms", source, ignore=shutil.ignore_patterns(*linked))
    for name in linked:
        (source / name).symlink_to(original / "agent_comms" / name, target_is_directory=True)
    (source.parent / "agent_comms_hooks").symlink_to(
        original / "agent_comms_hooks", target_is_directory=True)
    subprocess.run(("git", "init", "-q", str(checkout)), check=True, capture_output=True)
    fixture_git.run(checkout, "commit", "--allow-empty", "-qm", "Source fixture")
    return PristineProjection(checkout, projection_source_digest(source))


@pytest.fixture
def projection_source(pristine_projection: PristineProjection, tmp_path: Path) -> Path:
    """A private copy of the pristine commit, so its digest before any edit is the pristine one."""
    checkout = tmp_path / "producer-source"
    shutil.copytree(pristine_projection.checkout, checkout, symlinks=True)
    return checkout / "src" / "agent_comms"


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


def source_file(source: Path, owner: type) -> Path:
    """The private source's copy of the module that declares `owner`."""
    return source.joinpath(*owner.__module__.split(".")[1:]).with_suffix(".py")


def edit_inside(
    source: Path, owner: type, member: str, change: Callable[[ast.stmt], None],
) -> None:
    """Change an actual declaration in the private source, never a digest return value."""
    path = source_file(source, owner)
    module = ast.parse(path.read_text())
    declaration, = (node for node in module.body
                     if isinstance(node, ast.ClassDef) and node.name == owner.__name__)
    # @overload stubs bind the name for type checkers only; the runtime declaration is the edit.
    statement, = (node for node in declaration.body
                  if member in ProducedTable.bound_names(ast.unparse(node))
                  and not (isinstance(node, ast.FunctionDef)
                           and any(ast.unparse(decorator) == "overload"
                                   for decorator in node.decorator_list)))
    before = ast.unparse(statement)
    change(statement)
    assert ast.unparse(statement) != before, f"{owner.__name__}.{member}: the edit changed nothing"
    path.write_text(ast.unparse(ast.fix_missing_locations(module)) + "\n")


def return_no_rows(statement: ast.stmt) -> None:
    assert isinstance(statement, ast.FunctionDef)
    statement.body = [ast.Return(value=ast.Tuple(elts=[], ctx=ast.Load()))]


def route_the_message_itself(statement: ast.stmt) -> None:
    """A routing body no declared route produces, so the edit changes every owner."""
    assert isinstance(statement, ast.FunctionDef)
    statement.body = [ast.Return(value=ast.Tuple(
        elts=[ast.Name(id="message", ctx=ast.Load())], ctx=ast.Load()))]


def widen_spelling(statement: ast.stmt) -> None:
    assert isinstance(statement, ast.Assign)
    statement.value = ast.BinOp(left=statement.value, op=ast.Add(), right=ast.Constant(1))


def change_column_type(statement: ast.stmt) -> None:
    assert isinstance(statement, ast.AnnAssign)
    statement.annotation = ast.Name(id="str", ctx=ast.Load())


FIRST_POSITION_DIGEST = """
import importlib, inspect, json, sys
startup = sys.argv[1]
if startup != "table-first":
    importlib.import_module(startup)
from agent_comms.messaging.messages import TeamUpdatePosition
if startup == "table-first":
    # The cold direct call: nothing has completed the writer family yet.
    from agent_comms.messaging.projection_source import ProjectionSource
    ProjectionSource.writers_for(TeamUpdatePosition)
if startup == "agent_comms.cli_verbs.reconcile_stores":
    from agent_comms.producer_stamp import stamped_digests
    first = stamped_digests()[TeamUpdatePosition.declared_name]
else:
    first = TeamUpdatePosition.producer_digest()
from agent_comms.messaging.messages import BusRecord
from agent_comms.messaging.projection_source import ProjectionSource
writers = ProjectionSource.writers_for(TeamUpdatePosition)
print(json.dumps(dict(
    first=first, source=inspect.getfile(TeamUpdatePosition),
    kinds=sorted(kind.__name__ for kind in BusRecord.members_with(BusRecord) if kind in writers),
)))
"""
STARTUPS = (
    "table-first", "agent_comms.task_attachments.team_pings", "agent_comms.execution.worker",
    "agent_comms.cli_verbs.reconcile_stores",
)


def first_position_digest(source: Path, startup: str) -> dict:
    """The FIRST digest a fresh process computes for the team position, from one startup."""
    return json.loads(subprocess.run(
        (sys.executable, "-P", "-c", FIRST_POSITION_DIGEST, startup),
        check=True, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(source.parent)},
    ).stdout)


def test_the_first_team_position_digest_does_not_depend_on_import_order(
    projection_source: Path,
) -> None:
    """A table read before any task module is imported still commits to the task that
    produces it; every startup path stamps one digest and one writer family."""
    firsts = {startup: first_position_digest(projection_source, startup) for startup in STARTUPS}
    assert all(Path(first["source"]).is_relative_to(projection_source) for first in firsts.values())
    assert len({first["first"] for first in firsts.values()}) == 1
    assert {tuple(first["kinds"]) for first in firsts.values()} == {
        ("ClaimRecord", "KeyedPrivateRecord", "PrivateRecord", "PublicRecord")}
    edit_inside(projection_source, TeamUpdate, "projections", return_no_rows)
    for startup in STARTUPS[:2]:
        assert first_position_digest(projection_source, startup)["first"] != firsts[startup]["first"]


@pytest.mark.parametrize("owner, member", (
    (TypedTable, "select"),
    (AnsweredSource, "unanswered_references"),
    (TeamUpdatePosition, "latest"),
))
def test_projection_digest_ignores_reader_bodies(
    pristine_projection: PristineProjection, projection_source: Path, owner: type, member: str,
) -> None:
    before = pristine_projection.digest
    edit_inside(projection_source, owner, member, return_no_rows)
    assert projection_source_digest(projection_source) == before


@pytest.mark.parametrize("owner, changes", ((MessageIdText, True), (GitCommitText, False)))
def test_projection_digest_tracks_declared_column_spelling(
    pristine_projection: PristineProjection, projection_source: Path, owner: type, changes: bool
) -> None:
    before = json.loads(pristine_projection.digest)["message_identity_source"]
    edit_inside(projection_source, owner, "digits", widen_spelling)
    after = json.loads(projection_source_digest(projection_source))["message_identity_source"]
    assert (after != before) == changes


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


def test_projection_digest_tracks_actual_delivery_production(
    pristine_projection: PristineProjection, projection_source: Path,
) -> None:
    before = json.loads(pristine_projection.digest)
    edit_inside(projection_source, Delivered, "projections", return_no_rows)
    after = json.loads(projection_source_digest(projection_source))
    affected = {row.declared_name for row in Delivered.writes}
    assert affected
    assert all(after[name] != before[name] for name in affected)
    assert after[TeamUpdatePosition.declared_name] == before[TeamUpdatePosition.declared_name]


@pytest.mark.parametrize("owner", (MessageRoutes, ClaimNoticeKind))
def test_projection_digest_tracks_inherited_routing(
    pristine_projection: PristineProjection, projection_source: Path, owner: type,
) -> None:
    before = json.loads(pristine_projection.digest)
    edit_inside(projection_source, owner, "route_rows", route_the_message_itself)
    after = json.loads(projection_source_digest(projection_source))
    assert after[BusRouteCounts.declared_name] != before[BusRouteCounts.declared_name]
    assert after[AnsweredSource.declared_name] == before[AnsweredSource.declared_name]


def test_projection_digest_tracks_output_column_shape(
    pristine_projection: PristineProjection, projection_source: Path,
) -> None:
    before = json.loads(pristine_projection.digest)
    edit_inside(projection_source, AnsweredSource, "original", change_column_type)
    after = json.loads(projection_source_digest(projection_source))
    assert after[AnsweredSource.declared_name] != before[AnsweredSource.declared_name]
    assert after[TeamUpdatePosition.declared_name] == before[TeamUpdatePosition.declared_name]


def test_activity_source_changes_only_sent_activity(
    pristine_projection: PristineProjection, projection_source: Path,
) -> None:
    before = json.loads(pristine_projection.digest)
    path = source_file(projection_source, ConversationActivity)
    original = path.read_text()
    old = "return (SentActivity(message.sender, message.timestamp),)"
    assert original.count(old) == 1
    path.write_text(original.replace(
        old, "return (SentActivity(message.sender, message.timestamp + 1.0),)"))
    after = json.loads(projection_source_digest(projection_source))
    assert {name for name in before if before[name] != after[name]} == {"sent_activity"}


def test_display_import_does_not_add_projection_producers(
    pristine_projection: PristineProjection, projection_source: Path,
) -> None:
    before = pristine_projection.digest
    path = projection_source / "producer_stamp.py"
    original = path.read_text()
    declaration = "from agent_comms.messaging.bus import Bus as Bus"
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


def test_producer_digest_reads_routing_not_send_checks():
    """A send-time check produces no route row, so editing it rebuilds no history."""
    members = dict(BusRouteCounts.producer_members())
    assert MessageRoutes in BusRouteCounts.producer_owners()
    assert MessageKind not in BusRouteCounts.producer_owners()
    assert members[HandoffKind] == ()
    assert {
        name for member in members[ClaimNoticeKind]
        for name in BusRouteCounts.bound_names(member)
    } == {"conversation_message", "route_rows"}


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
def test_record_family_rebuild_and_history_install_preserve_scope(bus, declaration, tmp_path):
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
    kept = copied.lookup(record.seq)
    assert kept == record.history_record
    assert kept.publication_snapshot == record.history_snapshot.publication_snapshot
    assert copied.lookup_message(record.message_id) == kept
    # A delivered record keeps its frozen audience, so its recipient still received it.
    assert copied.received("b" * 32) == record.deliveries()
    assert copied.page(record.seq + 1, 10) == (kept, copied.lookup(original.seq))
    assert copied.replies((original.reference,)) == (kept,)


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
        Message("owner", "colleague", "Recorded directive", InfoKind, seq=seq)))
    with pytest.raises(RelationViolationError):
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("colleague", "#comms", "Wrong original reference", InfoKind, seq=seq,
                    answers=(MessageReference(
                        original.seq + offset, MessageId.mint("wrong-hash")),))))
    assert bus.revision() == original.seq
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("another-teammate", "#comms", "Someone else's answer", InfoKind, seq=seq,
                answers=(original.reference,))))
    assert bus.unanswered_references((original.seq,), AuthoredAnswers(frozenset({"colleague"}))) == (original.reference,)
    assert bus.unanswered_references((original.seq,)) == ()
    with bus.reading() as before_reply:
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("renamed-colleague", "#comms", "The owner's answer", InfoKind, seq=seq,
                    answers=(original.reference,))))
        assert before_reply.unanswered_references((original.seq,), AuthoredAnswers(frozenset({"colleague", "renamed-colleague"}))) == (
            original.reference,)
    assert bus.unanswered_references((original.seq,), AuthoredAnswers(frozenset({"colleague", "renamed-colleague"}))) == ()
    with pytest.raises(RowNotFoundError):
        bus.unanswered_references((original.seq, bus.revision() + 1), AuthoredAnswers(frozenset({"colleague"})))


def test_answer_projection_rebuild_preserves_sequence_links_and_public_references(bus):
    original = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("owner", "colleague", "Recorded question", InfoKind, seq=seq)))
    reply = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("colleague", "owner", "Recorded answer", InfoKind, seq=seq,
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
    assert bus.unanswered_references((original.seq,), AuthoredAnswers(frozenset({"colleague"}))) == ()


def team_packet(bus, holder, body):
    return bus.append(lambda seq, db: PublicRecord.from_message(Message(
        "user", holder.name, body, InfoKind, seq=seq,
        task=TeamUpdateNotice(holder, ()))))


def test_team_packet_positions_append_with_their_message_and_rebuild_from_it(bus, tmp_path):
    """Each typed packet's position is written in its own append; a taskless message from the
    same sender writes none, and only that table rebuilds from the original records."""
    colleague, other = RecordedThreadIncarnation("colleague", 1.0), RecordedThreadIncarnation("other", 2.0)
    first = team_packet(bus, colleague, "first board")
    raw = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("user", "colleague", "independent instruction", InfoKind, seq=seq)))
    theirs = team_packet(bus, other, "another teammate's board")

    def positions():
        with bus.observing() as db:
            return TeamUpdatePosition.select(db, order_by=("seq",))

    appended = positions()
    assert appended == [TeamUpdatePosition(first.seq, colleague.lookup),
                        TeamUpdatePosition(theirs.seq, other.lookup)]
    with bus.observing() as db:
        assert TeamUpdatePosition.latest(db, colleague.lookup) == (first,)
        assert TeamUpdatePosition.latest(db, other.lookup) == (theirs,)
        assert TeamUpdatePosition.latest(db, RecordedThreadIncarnation("colleague", 3.0).lookup) == ()
    with bus.writer_connection() as db:
        ProjectionProducer.update(
            db, where="{projection}=?", parameters=(TeamUpdatePosition.declared_name,),
            producer_digest="0" * 64,
        )
        db.execute(f'DELETE FROM "{TeamUpdatePosition.declared_name}"')
        assert BusSchema.reconcile_schema(db, WriterOwnedFile())
        assert TeamUpdatePosition.select(db, order_by=("seq",)) == appended
        before = db.total_changes
        assert not BusSchema.reconcile_schema(db, WriterOwnedFile())
        assert db.total_changes == before
    with bus.writing() as db:
        BusProjectionTables.reset_schema(db)
    Bus.install(bus.path)
    assert positions() == appended
    assert tuple(bus.lookup(record.seq) for record in (first, raw, theirs)) == (first, raw, theirs)
    # A new bus installed from this history indexes the same packets from their copies.
    copied = Bus.install(Bus.path_in(tmp_path / "history"), history=(first, raw, theirs))
    with copied.observing() as db:
        assert TeamUpdatePosition.select(db, order_by=("seq",)) == appended
        assert TeamUpdatePosition.latest(db, colleague.lookup) == (first.history_record,)
        assert TeamUpdatePosition.latest(db, other.lookup) == (theirs.history_record,)
    for original in (first, raw, theirs):
        kept = copied.lookup(original.seq)
        assert (kept.reference, kept.body, kept.task) == (original.reference, original.body, original.task)


def test_a_late_constraint_refusal_rolls_back_the_team_position(tmp_path, bus):
    """The position is written inside the packet's own transaction; a real SQLite refusal
    after it takes the header, task and position back together."""
    colleague = RecordedThreadIncarnation("colleague", 1.0)
    written = []

    def build(seq, db):
        record_from(PrivateRecord, Message(
            "user", "colleague", "not committed", InfoKind, seq=seq,
            task=TeamUpdateNotice(colleague, ())),
        ).append_to(db, seq)
        written.append((seq, TeamUpdatePosition.select(db)))
        BusRow.require(db, seq=seq).insert(db)

    with pytest.raises(HumanSendRefusedError) as refused:
        HumanOrigin("author", 1, Checkout(tmp_path)).append(bus, build)
    cause = refused.value.__cause__
    assert type(cause) is sqlite3.IntegrityError
    assert "UNIQUE constraint failed: bus.seq" in str(cause)
    (seq, positions), = written
    assert positions == [TeamUpdatePosition(seq, colleague.lookup)]
    with closing(sqlite3.connect(bus.path)) as db:
        assert TeamUpdatePosition.count(db) == 0
        assert all(table.count(db) == 0 for table in TypedTable.members_with(BusTables))


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
    origin = HumanOrigin("author", 1, Checkout(tmp_path))
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
    marker, = bus.source_paths(bus.path.parent)
    marker.mkdir(parents=True)
    origin = HumanOrigin("author", 1, Checkout(tmp_path))
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


def test_human_programming_failure_propagates_after_rollback(tmp_path, bus):
    failure = RuntimeError("Broken publication implementation")

    def build(seq, db):
        record_from(
            PrivateRecord, Message("author", "worker", "not committed", InfoKind, seq=seq)
        ).append_to(db, seq)
        raise failure

    with pytest.raises(RuntimeError) as failed:
        HumanOrigin("author", 1, Checkout(tmp_path)).append(bus, build)
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



def test_moving_a_producer_module_to_another_package_keeps_every_producer_digest(
    pristine_projection: PristineProjection, projection_source: Path,
) -> None:
    """A package move changes where a producer's Python file lives, not what it writes. On
    2026-11-04 #4084's storage move made all 13 bus tables 'stored rows of an earlier
    producer' and cost a 128 s rehearsal rebuild."""
    checkout = projection_source.parents[1]
    # The codemod moves with `git mv`, so the copy's files are tracked first.
    subprocess.run(("git", "-C", str(checkout), "add", "-A"), check=True, capture_output=True)
    package = ConversationActivity.__module__.split(".")[1]
    subprocess.run((sys.executable, str(Path(__file__).resolve().parents[1] / "tools" /
                    "move_flat_modules.py"), str(checkout), "relocated", package),
                   check=True, capture_output=True, timeout=120)
    assert source_file(projection_source / "relocated", ConversationActivity).exists()
    assert projection_source_digest(projection_source) == pristine_projection.digest
