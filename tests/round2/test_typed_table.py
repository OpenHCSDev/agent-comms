"""Declaration families round-trip through real SQLite without a schema roster."""

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Annotated, ClassVar, Type

import pytest

from agent_comms.field_codec import FieldCodec, MalformedRecord, NullMember
from agent_comms.messages import InfoKind, MessageKind
from agent_comms.typed_table import (
    Column,
    ExactStorage,
    ForeignKey,
    Generated,
    Index,
    IntegerStorage,
    SqlStorage,
    SqlValue,
    SQLiteSchemaObject,
    TypedRow,
    TypedTable,
    RowNotFoundError,
)


@dataclass(frozen=True)
class TableParentRow(TypedTable):
    key: Annotated[str, Column(primary_key=True)]
    count: int = 0
    unique = (("key", "count"),)


@dataclass(frozen=True)
class TableChildRow(TypedTable):
    key: Annotated[str, Column(primary_key=True)]
    parent: str
    enabled: bool
    detail: tuple[str, ...]
    weight: float | None
    parent_count: int = 3
    indexes = (Index(("parent", "enabled")),)
    without_rowid = True

    @classmethod
    def references(cls):
        return (ForeignKey(columns=("parent", "parent_count"), target=TableParentRow, target_columns=("key", "count")),)


def test_required_row_distinguishes_absence_from_duplicate_results():
    with sqlite3.connect(":memory:") as db:
        TableParentRow.create(db)
        with pytest.raises(RowNotFoundError):
            TableParentRow.require(db, key="missing")
        first, second = TableParentRow("first", 3), TableParentRow("second", 3)
        first.insert(db)
        second.insert(db)
        assert TableParentRow.require(db, key=first.key) == first
        with pytest.raises(MalformedRecord) as duplicate:
            TableParentRow.require(db, count=3)
        assert type(duplicate.value) is MalformedRecord


def test_declared_table_family(tmp_path):
    db_path = tmp_path / "family.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for member in TypedTable.members_with(TypedTable):
            if member.__module__ == __name__:
                member.create(db)
                columns = db.execute(f'PRAGMA table_info("{member.declared_name}")').fetchall()
                assert tuple(column[1] for column in columns) == member.columns()
        parent = TableParentRow("p", 3)
        child = TableChildRow("c", "p", True, ("one", "two"), None)
        for row in (parent, child):
            row.insert(db)
            assert type(row).select(db) == [row]
        with pytest.raises(sqlite3.IntegrityError):
            replace(child, key="bad", parent="missing").insert(db)
        with pytest.raises(TypeError):
            replace(parent, key="bool", count=True).insert(db)
        replace(child, weight=3.25).upsert(db)
        assert TableChildRow.one(db, key="c").weight == 3.25
        child.upsert(db)
        assert TableChildRow.one(db, key="c") == child
        TableChildRow.update(db, where="key=?", parameters=("c",), enabled=False)
        assert TableChildRow.select(db, where="enabled=?", parameters=(False,),
                                    order_by=("key",)) == [replace(child, enabled=False)]
        assert TableChildRow.one(db) == replace(child, enabled=False)
        with pytest.raises(ValueError):
            TableChildRow.select(db, missing=1)
        with pytest.raises(ValueError):
            TableChildRow.update(db, where="key=?", parameters=("c",))
        with pytest.raises(ValueError):
            TableChildRow._column_list(())
        with pytest.raises(ValueError):
            TableChildRow.update(db, where="key=?", parameters=("c",), missing=1)
        with pytest.raises(TypeError):
            TableChildRow.update(db, where="key=?", parameters=("c",), enabled=1)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        assert TableChildRow.select(db) == [replace(child, enabled=False)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE table_child SET enabled=2")
        db.rollback()
        db.execute("BEGIN")
        TableChildRow.update(db, where="key=?", parameters=("c",), weight=2.5)
        db.rollback()
        assert TableChildRow.select(db)[0].weight is None


def test_rebuild_converts_a_field_and_preserves_the_declared_rows_and_constraints():
    with sqlite3.connect(':memory:') as db:
        db.execute('PRAGMA foreign_keys=ON')
        TableParentRow.create(db)
        TableChildRow.create(db)
        TableParentRow('p', 3).insert(db)
        rows = (TableChildRow('first', 'p', True, ('one',), 125.0),
                TableChildRow('second', 'p', False, ('two',), 250.0))
        for row in rows:
            row.insert(db)
        db.execute(f'ALTER TABLE "{TableChildRow.declared_name}" RENAME COLUMN weight TO retired_weight')
        TableChildRow.rebuild(db, {'weight': 'retired_weight / 100.0'})
        assert TableChildRow.select(db, order_by=('key',)) == [
            replace(row, weight=row.weight / 100) for row in rows]
        assert not SQLiteSchemaObject.differences(
            TableChildRow.stored_objects(db), TableChildRow.schema_objects())
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            replace(rows[0], key='invalid', parent='missing').insert(db)
        db.execute(f'ALTER TABLE "{TableChildRow.declared_name}" DROP COLUMN weight')
        TableChildRow.rebuild(db)
        assert TableChildRow.select(db, order_by=('key',)) == [replace(row, weight=None) for row in rows]


def test_writer_derives_missing_nullable_fields_but_refuses_unclaimed_data():
    from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryError
    from agent_comms.goals import NoGoal
    from agent_comms.typed_table import WriterOwnedFile

    entry = GoalHistoryEntry(1, 'baseline', 1.0, NoGoal(), NoGoal(),
                             owner_created_at=1.0, state='committed')
    with sqlite3.connect(':memory:') as db:
        GoalHistoryEntry.create(db)
        entry.insert(db)
        for column in ('request_before', 'request_after'):
            db.execute(f'ALTER TABLE "{GoalHistoryEntry.declared_name}" DROP COLUMN {column}')
        db.commit()
        GoalHistoryEntry.reconcile_schema(db, WriterOwnedFile())
        assert GoalHistoryEntry.require(db, sequence=1) == entry
        db.execute(f'ALTER TABLE "{GoalHistoryEntry.declared_name}" ADD COLUMN unclaimed TEXT')
        db.execute(f'UPDATE "{GoalHistoryEntry.declared_name}" SET unclaimed=?', ('preserve this',))
        db.commit()
        with pytest.raises(GoalHistoryError):
            GoalHistoryEntry.reconcile_schema(db, WriterOwnedFile())
        assert db.execute(f'SELECT unclaimed FROM "{GoalHistoryEntry.declared_name}"').fetchall() == [('preserve this',)]


def test_durable_writer_relaxes_not_null_without_losing_rows():
    from agent_comms.typed_table import DurableTables, WriterOwnedFile

    @dataclass(frozen=True)
    class RelaxedValueRow(DurableTables, TypedTable, declared_name="relaxed_values"):
        key: Annotated[str, Column(primary_key=True)]
        value: str | None

    rows = [RelaxedValueRow("first", "one"), RelaxedValueRow("second", "two")]
    with sqlite3.connect(":memory:") as db:
        db.execute(
            'CREATE TABLE "relaxed_values" ("key" TEXT NOT NULL, "value" TEXT NOT NULL, '
            'PRIMARY KEY ("key")) STRICT'
        )
        for row in rows:
            row.insert(db)
        db.commit()

        RelaxedValueRow.reconcile_schema(db, WriterOwnedFile())

        assert RelaxedValueRow.select(db, order_by=("key",)) == rows
        RelaxedValueRow("empty", None).insert(db)
        assert RelaxedValueRow.require(db, key="empty").value is None
        assert RelaxedValueRow.count(db) == len(rows) + 1


def test_atomic_creation_preserves_declared_indexes_triggers_and_strict_creation():
    from agent_comms.native_pi import NativeContextJournal

    with sqlite3.connect(':memory:') as db:
        NativeContextJournal.create(db, if_not_exists=True)
        NativeContextJournal.create(db, if_not_exists=True)
        actual = SQLiteSchemaObject.read(db.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert SQLiteSchemaObject.differences(actual, NativeContextJournal.schema_objects()) == ()
        with pytest.raises(sqlite3.OperationalError):
            NativeContextJournal.create(db)


def test_index_owns_unique_partial_creation():
    with sqlite3.connect(':memory:') as db:
        TableParentRow.create(db)
        index = Index(('count',), unique=True, where='count > 0')
        statement = index.sql(TableParentRow, if_not_exists=True)
        db.execute(statement)
        db.execute(statement)
        TableParentRow('first', 1).insert(db)
        with pytest.raises(sqlite3.IntegrityError):
            TableParentRow('duplicate', 1).insert(db)
        TableParentRow('zero-one', 0).insert(db)
        TableParentRow('zero-two', 0).insert(db)
        with pytest.raises(sqlite3.OperationalError):
            db.execute(index.sql(TableParentRow))


def test_adding_an_index_renames_no_other_index():
    @dataclass(frozen=True)
    class IndexedRow(TypedTable):
        name: Annotated[str, Column(primary_key=True)]
        count: int
        indexes = (Index(('count',)), Index(('name', 'count'), where='count > 0'))

    @dataclass(frozen=True)
    class MoreIndexedRow(TypedTable, declared_name=IndexedRow.declared_name + '_more'):
        name: Annotated[str, Column(primary_key=True)]
        count: int
        indexes = (Index(('name',), unique=True), *IndexedRow.indexes)

    def names(row):
        return {index.term_sql(row): index.name(row) for index in row.indexes}

    before, after = names(IndexedRow), names(MoreIndexedRow)
    assert {sql: name.replace(IndexedRow.declared_name, '', 1) for sql, name in before.items()} == {
        sql: after[sql].replace(MoreIndexedRow.declared_name, '', 1) for sql in before}
    assert len(set(after.values())) == len(MoreIndexedRow.indexes)


def test_new_row_declaration_needs_no_other_edit():
    @dataclass(frozen=True)
    class AddedTableRow(TypedTable):
        label: Annotated[str, Column(unique=True, index=True)]
        child: TableChildRow
        event: Type[MessageKind] = InfoKind
        representation: type[SqlStorage] = IntegerStorage
        exact: Annotated[int, Column(storage=ExactStorage)] = 7
        ordinal: Annotated[int | None, Column(primary_key=True, auto_increment=True)] = field(default=None, compare=False)
        label_size: ClassVar[Generated] = Generated(int, Column(generated="length(label)"))

    @dataclass(frozen=True)
    class Projection(TypedRow):
        label: str
        enabled: bool

    with sqlite3.connect(":memory:") as db:
        AddedTableRow.create(db)
        value = AddedTableRow("new", TableChildRow("a", "b", True, (), 1.25))
        value.insert(db)
        assert AddedTableRow.select(db) == [value]
        stored = AddedTableRow.one(db, label="new")
        assert stored.ordinal == 1 and AddedTableRow.count(db, label_size=3) == 1
        with pytest.raises(AttributeError):
            stored.label_size
        assert stored.representation is IntegerStorage and stored.event is InfoKind
        assert AddedTableRow.count(db, exact=7) == 1
        with pytest.raises(ValueError):
            AddedTableRow.insertion_statement(("label_size",))
        with pytest.raises(ValueError):
            AddedTableRow.update(db, where="label=?", parameters=("new",), label_size=4)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE added_table SET event='invented'")
        db.execute("UPDATE added_table SET exact='7'")
        with pytest.raises(ValueError):
            AddedTableRow.one(db, label="new")
        AddedTableRow.update(db, where="label=?", parameters=("new",), exact=7)
        assert Projection.read(db.execute("SELECT 1 AS enabled, 'new' AS label")) == [
            Projection("new", True)
        ]
        with pytest.raises(ValueError):
            Projection.read(db.execute("SELECT 2 AS enabled, 'new' AS label"))
        with pytest.raises(ValueError):
            Projection.read(db.execute("SELECT 'new' AS label"))
        with pytest.raises(ValueError):
            Projection.read(db.execute("SELECT 'a' AS label, 'b' AS label"))
        with pytest.raises(MalformedRecord):
            AddedTableRow.update(db, where="1", child=FieldCodec.decode(
                TableChildRow, {TableChildRow.family_discriminator: TableChildRow.declared_name}
            ))


@dataclass(frozen=True)
class NoTableNote(NullMember):
    """The row was stored without a note."""


def test_null_member_column_stores_sql_null():
    @dataclass(frozen=True)
    class NotedTableRow(TypedTable):
        key: Annotated[str, Column(primary_key=True)]
        note: str | NoTableNote = field(default_factory=NoTableNote)

    with sqlite3.connect(":memory:") as db:
        NotedTableRow.create(db)
        assert '"note" TEXT,' in NotedTableRow.ddl()[0] + ","
        NotedTableRow("silent").insert(db)
        NotedTableRow("said", "hello").insert(db)
        assert db.execute("SELECT key FROM noted_table WHERE note IS NULL").fetchall() == [("silent",)]
        assert NotedTableRow.one(db, key="silent") == NotedTableRow("silent")
        assert NotedTableRow.one(db, key="said").note == "hello"


def test_generated_column_is_declared_by_the_table_not_a_record_field():
    @dataclass(frozen=True)
    class FieldGeneratedTableRow(TypedTable, declared_name=None):
        label: str
        label_size: Annotated[int, Column(generated="length(label)")] = field(init=False)

    with pytest.raises(TypeError):
        FieldGeneratedTableRow.ddl()


def test_declared_values_and_partial_insertions_share_the_sqlite_boundary():
    with sqlite3.connect(":memory:") as db:
        assert not TableParentRow.present(db)
        TableParentRow.create(db)
        assert TableParentRow.present(db)
        assert not TableParentRow.exists(db)
        TableParentRow.insert_values(db, key="parent", count=3)
        assert TableParentRow.exists(db, where="key=?", parameters=("parent",))
        assert TableParentRow("parent", 4).insert_if_absent(db).rowcount == 0
        assert TableParentRow.one(db, key="parent").count == 3
        assert SqlValue.for_annotation(int).read(db.execute("SELECT count(*) FROM table_parent")) == [1]
        assert SqlValue.for_annotation(int | None).read(db.execute("SELECT NULL")) == [None]
        assert SqlValue.for_annotation(int).read(db.execute("SELECT 1 WHERE 0")) == []
        assert SqlValue.for_annotation(bool).read(db.execute("SELECT 1")) == [True]
        cursor = db.execute("SELECT 2")
        with pytest.raises(ValueError):
            SqlValue.for_annotation(bool).read(cursor)
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchone()
        with pytest.raises(ValueError):
            SqlValue.for_annotation(int).read(db.execute("SELECT 1, 2"))
        with pytest.raises(ValueError):
            TableParentRow.insert_values(db, invented="field")


def test_streamed_query_decodes_only_consumed_rows_and_releases_cursor():
    @dataclass(frozen=True)
    class StreamProjection(TypedRow):
        enabled: bool

    with sqlite3.connect(":memory:") as db:
        cursor = db.execute("SELECT 1 AS enabled UNION ALL SELECT 2 AS enabled")
        rows = StreamProjection.iterate(cursor)
        assert next(rows) == StreamProjection(True)
        rows.close()
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchone()
        rows = StreamProjection.iterate(db.execute("SELECT 2 AS enabled"))
        with pytest.raises(ValueError):
            next(rows)
