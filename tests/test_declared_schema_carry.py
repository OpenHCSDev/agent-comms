"""The carry gives every durable file kind its declared schema and keeps every row."""

from contextlib import closing
import json
import re
import sqlite3
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.errors import RelationViolationError
from agent_comms.goals import OrderCheck
from agent_comms.installer.carry import CarryChain
from agent_comms.native_pi import NativeContextJournal
from agent_comms.native_session_files import NativeSessionFiles, PrivateSession
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registration import Registration
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import GoalRef, MechanicalOrderRun, PendingProblemOrder, Todo, TodoStore
from agent_comms.typed_table import DurableTables, SharedFileTables
from tools.cutover.carry_declared_schema import _subclasses, carry

REPO = Path(__file__).resolve().parents[1]


def _file_rows():
    """Each durable file's rows: the shared coordinator file is one file of many kinds."""
    kinds = {kind for kind in _subclasses(DurableTables)
             if not any(issubclass(base, DurableTables) for base in kind.__bases__ if base is not DurableTables)}
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


def _positional_indexes(db, row):
    """Rename a row's declared indexes to the positional names older files hold."""
    indexes = [(name, sql) for name, sql in row.schema_objects().items() if _INDEX.match(sql)]
    for ordinal, (name, sql) in enumerate(indexes):
        db.execute(f'DROP INDEX "{name}"')
        db.execute(_INDEX.sub(rf'\1"{row.declared_name}_{ordinal}_idx"\2', sql))


def test_the_schema_carry_runs_after_the_review_timer_carry():
    """The todo file's deadline column is renamed before its schema is compared."""
    names = [step.name() for step in CarryChain.at(REPO).steps]
    assert names.index("carry_review_timers") < names.index("carry_declared_schema")


def test_a_bus_with_a_todo_deadline_and_old_proofs_carries_through_the_whole_chain(tmp_path):
    lead = Registration(tmp_path / "registry.json").new_thread(
        "lead", frozenset(), str(tmp_path), created_at=1.0)
    order = PendingProblemOrder(
        id="checked-order", owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(tmp_path), "true"), edits=("Fix the problem",),
        banned=("No extra store",), report="Report the delivered commit",
        at=RecordedEventTime(10.0), first_call_deadline=RecordedEventTime(130.0))
    created = TodoStore(tmp_path).create(
        "todo-1", RepositoryName.from_wire("OpenHCSDev/agent-comms"), "Fix the sidebar",
        creator=lead, goal=GoalRef("lead", 1.0, "goal-1"), owner=lead, generation="initial",
        review_at=RecordedEventTime(1000.0), order=order)
    with closing(sqlite3.connect(TodoStore.path_in(tmp_path))) as db:
        db.execute('ALTER TABLE "todo" RENAME COLUMN "review_at" TO "deadline"')
        db.commit()
    proof = NativeSessionFiles(PrivateSession.path_in(tmp_path, session_id="s")).input_proof
    proof.parent.mkdir()
    proof.with_suffix("").write_text("")
    with closing(sqlite3.connect(proof)) as db:
        NativeContextJournal.create(db)
        _positional_indexes(db, NativeContextJournal)
        db.commit()
        assert NativeContextJournal.schema_differs(db)

    for step in CarryChain.at(REPO).steps:
        step.carry(tmp_path, apply=True)

    assert TodoStore(tmp_path).get("todo-1").review_at == created.review_at
    with closing(sqlite3.connect(TodoStore.path_in(tmp_path))) as db:
        assert "deadline" not in Todo.stored_columns(db)
    with closing(sqlite3.connect(proof)) as db:
        assert not NativeContextJournal.schema_differs(db)
