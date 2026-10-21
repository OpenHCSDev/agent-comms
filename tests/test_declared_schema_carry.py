"""The carry gives every durable file kind its declared schema and keeps every row."""

from contextlib import closing
import json
import re
import sqlite3

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.typed_table import DurableTables, SharedFileTables, TypedTable
from tools.cutover.declared_schema import _subclasses, carry


def _file_rows():
    """Each durable file's rows: the shared coordinator file is one file of many kinds."""
    kinds = {kind for kind in _subclasses(DurableTables)
             if not issubclass(kind, TypedTable) or kind.schema_rows() == (kind,)}
    shared = tuple({row for kind in kinds if issubclass(kind, SharedFileTables)
                    for row in kind.schema_rows()})
    files = {kind.__name__: kind.schema_rows() for kind in kinds
             if not issubclass(kind, SharedFileTables)}
    return {"coordinator": shared, **files}


FILES = _file_rows()
_INDEX = re.compile(r'^(CREATE (?:UNIQUE )?INDEX )"[^"]+"( ON )')
# Each column's stored type answers a value STRICT accepts; checks are not enforced while
# the old file is written, so one row reaches every table whose generated columns allow it.
_VALUES = {"TEXT": "x", "INTEGER": 1, "REAL": 1.0, "BLOB": b"x", "": 1, "ANY": 1}
_EXTRACT = re.compile(r'"\w+" (\w+) GENERATED ALWAYS AS \(json_extract\((\w+), ?\'\$\.([\w.]+)\'\)\)')


def _stand_in(sql, column, storage):
    """A TEXT column a generated column reads as JSON holds each path it reads."""
    document = {"kind": "x"}
    for kind, source, path in _EXTRACT.findall(sql):
        if source == column:
            *parents, leaf = path.split(".")
            node = document
            for name in parents:
                node = node.setdefault(name, {})
            node[leaf] = _VALUES[kind]
    return json.dumps(document) if storage == "TEXT" else _VALUES[storage]


def _old_file(path, rows):
    """The file as positional names and a sealed schema copy left it, with rows."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA ignore_check_constraints=1")
        for row in rows:
            db.execute(row.schema_objects()[row.declared_name])
        for row in rows:
            columns = [column for column in db.execute(f'PRAGMA table_xinfo("{row.declared_name}")')
                       if column[6] == 0]
            try:
                db.execute(
                    f'INSERT INTO "{row.declared_name}" ({",".join(chr(34) + c[1] + chr(34) for c in columns)})'
                    f' VALUES ({",".join("?" for _ in columns)})', [_stand_in(row.schema_objects()[row.declared_name], c[1], c[2]) for c in columns])
            except sqlite3.IntegrityError:
                pass  # a generated NOT NULL column refuses the stand-in value
        for row in rows:
            indexes = [sql for name, sql in row.schema_objects().items() if name != row.declared_name
                       and _INDEX.match(sql)]
            for ordinal, sql in enumerate(indexes):
                db.execute(_INDEX.sub(rf'\1"{row.declared_name}_{ordinal}_idx"\2', sql))
            for name, sql in row.schema_objects().items():
                if name != row.declared_name and not _INDEX.match(sql):
                    db.execute(sql)
        db.execute("CREATE TABLE cohort_schema_meta (singleton INTEGER PRIMARY KEY, ddl_digest TEXT)")
        db.execute("INSERT INTO cohort_schema_meta VALUES (1, 'digest')")
        db.execute("CREATE TRIGGER cohort_meta_update_guard BEFORE UPDATE ON cohort_schema_meta "
                   "BEGIN SELECT RAISE(ABORT, 'frozen'); END")
        db.execute("PRAGMA user_version=11")
        db.commit()


def _rows(path, rows):
    with closing(sqlite3.connect(path)) as db:
        return {row.declared_name: sorted(map(repr, db.execute(f'SELECT * FROM "{row.declared_name}"')))
                for row in rows}


@pytest.mark.parametrize("file", sorted(FILES))
def test_carry_opens_an_old_file_with_identical_rows(tmp_path, file):
    rows = FILES[file]
    path = tmp_path / "old.sqlite3"
    _old_file(path, rows)
    stored = _rows(path, rows)
    assert any(stored.values())
    carried = carry(path)
    assert set(carried.renamed().values()) == {
        name for row in rows for name, sql in row.schema_objects().items() if _INDEX.match(sql)}
    assert carried.retired == {"cohort_schema_meta": 1}
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'cohort_meta%' OR name LIKE '%schema_meta'"
        ).fetchall()
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        for row in rows:
            assert {item.name: item.sql for item in row.stored_objects(db)} == row.schema_objects()
    assert _rows(path, rows) == stored
    again = carry(path)
    assert not again.renamed() and again.before == again.after
    assert _rows(path, rows) == stored


def _refused(path, statement, match):
    with closing(sqlite3.connect(path)) as db:
        db.execute(statement)
        db.commit()
    before = path.read_bytes()
    with pytest.raises(RelationViolationError, match=match):
        carry(path)
    assert path.read_bytes() == before


def test_carry_refuses_an_undeclared_table(tmp_path):
    _refused(tmp_path / "f.sqlite3", "CREATE TABLE unknown_rows (x INTEGER)", "undeclared table")


def test_carry_refuses_a_table_whose_columns_differ(tmp_path):
    _refused(tmp_path / "f.sqlite3", 'CREATE TABLE "binding" ("root" TEXT NOT NULL) STRICT',
             "columns differ")


def test_carry_refuses_a_derived_file(tmp_path):
    from agent_comms.bus_page_index import BusPageIndex

    row = BusPageIndex.rows[0]
    _refused(tmp_path / "f.sqlite3", row.schema_objects()[row.declared_name], "derived")
