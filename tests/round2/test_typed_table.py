"""Declaration families round-trip through real SQLite without a schema roster."""

import sqlite3
from dataclasses import dataclass, field, replace

import pytest

from agent_comms.typed_table import Column, ForeignKey, Index, TypedRow, TypedTable


@dataclass(frozen=True)
class TableParentRow(TypedTable):
    key: str = field(metadata={"sql": Column(primary_key=True)})
    count: int = 0
    unique = (("key", "count"),)


@dataclass(frozen=True)
class TableChildRow(TypedTable):
    key: str = field(metadata={"sql": Column(primary_key=True)})
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
        with pytest.raises(ValueError):
            replace(parent, key="bool", count=True).insert(db)
        TableChildRow.update(db, where="key=?", parameters=("c",), enabled=False)
        with pytest.raises(ValueError):
            TableChildRow.update(db, where="key=?", parameters=("c",), missing=1)
        with pytest.raises(ValueError):
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


def test_new_row_declaration_needs_no_other_edit():
    @dataclass(frozen=True)
    class AddedTableRow(TypedTable):
        label: str = field(metadata={"sql": Column(primary_key=True, index=True)})
        child: TableChildRow

    @dataclass(frozen=True)
    class Projection(TypedRow):
        label: str
        enabled: bool

    with sqlite3.connect(":memory:") as db:
        AddedTableRow.create(db)
        value = AddedTableRow("new", TableChildRow("a", "b", True, (), 1.25))
        value.insert(db)
        assert AddedTableRow.select(db) == [value]
        assert Projection.read(db.execute("SELECT 1 AS enabled, 'new' AS label")) == [
            Projection("new", True)
        ]
        with pytest.raises(ValueError):
            Projection.read(db.execute("SELECT 2 AS enabled, 'new' AS label"))
        with pytest.raises(ValueError):
            Projection.read(db.execute("SELECT 'new' AS label"))
        with pytest.raises(ValueError):
            Projection.read(db.execute("SELECT 'a' AS label, 'b' AS label"))
        with pytest.raises(TypeError):
            AddedTableRow.update(db, where="1", child={"kind": "table_child"})
