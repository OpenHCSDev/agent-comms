"""Declaration families round-trip through real SQLite without a schema roster."""

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Annotated, Type

import pytest

from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.messages import InfoKind, MessageKind
from agent_comms.typed_table import (
    Column,
    ExactStorage,
    ForeignKey,
    Index,
    IntegerStorage,
    SqlStorage,
    SqlValue,
    TypedRow,
    TypedTable,
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
        return (ForeignKey(("parent", "parent_count"), TableParentRow, ("key", "count")),)


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
        with pytest.raises(ValueError, match="Unknown fields"):
            TableChildRow.select(db, missing=1)
        with pytest.raises(ValueError, match="Update requires fields"):
            TableChildRow.update(db, where="key=?", parameters=("c",))
        with pytest.raises(ValueError, match="Empty column list"):
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


def test_atomic_creation_preserves_declared_indexes_triggers_and_strict_creation():
    from agent_comms.native_pi import NativeContextJournal
    from agent_comms.typed_table import SQLiteSchemaObject

    with sqlite3.connect(':memory:') as db:
        NativeContextJournal.create(db, if_not_exists=True)
        NativeContextJournal.create(db, if_not_exists=True)
        actual = SQLiteSchemaObject.read(db.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert SQLiteSchemaObject.differences(actual, NativeContextJournal.schema_objects()) == ()
        with pytest.raises(sqlite3.OperationalError, match='already exists'):
            NativeContextJournal.create(db)


def test_index_owns_unique_partial_creation():
    with sqlite3.connect(':memory:') as db:
        TableParentRow.create(db)
        index = Index(('count',), unique=True, where='count > 0')
        statement = index.sql(TableParentRow, 'positive_counts', if_not_exists=True)
        db.execute(statement)
        db.execute(statement)
        TableParentRow('first', 1).insert(db)
        with pytest.raises(sqlite3.IntegrityError):
            TableParentRow('duplicate', 1).insert(db)
        TableParentRow('zero-one', 0).insert(db)
        TableParentRow('zero-two', 0).insert(db)
        with pytest.raises(sqlite3.OperationalError, match='already exists'):
            db.execute(index.sql(TableParentRow, 'positive_counts'))


def test_new_row_declaration_needs_no_other_edit():
    @dataclass(frozen=True)
    class AddedTableRow(TypedTable):
        label: Annotated[str, Column(unique=True, index=True)]
        child: TableChildRow
        event: Type[MessageKind] = InfoKind
        representation: type[SqlStorage] = IntegerStorage
        exact: Annotated[int, Column(storage=ExactStorage)] = 7
        ordinal: Annotated[int | None, Column(primary_key=True, auto_increment=True)] = field(default=None, compare=False)
        label_size: Annotated[int | None, Column(generated="length(label)")] = field(default=None, init=False, compare=False)

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
        assert stored.ordinal == 1 and stored.label_size == 3
        assert stored.representation is IntegerStorage and stored.event is InfoKind
        assert AddedTableRow.count(db, exact=7) == 1
        with pytest.raises(ValueError, match="Generated columns"):
            AddedTableRow.insertion_statement(("label_size",))
        with pytest.raises(ValueError, match="Generated columns"):
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
        with pytest.raises(MalformedRecord, match="Missing required fields for TableChildRow"):
            AddedTableRow.update(db, where="1", child=FieldCodec.decode(
                TableChildRow, {TableChildRow.family_discriminator: TableChildRow.declared_name}
            ))


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
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
        with pytest.raises(ValueError, match="exactly one"):
            SqlValue.for_annotation(int).read(db.execute("SELECT 1, 2"))
        with pytest.raises(ValueError, match="Unknown fields"):
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
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
        rows = StreamProjection.iterate(db.execute("SELECT 2 AS enabled"))
        with pytest.raises(ValueError):
            next(rows)
