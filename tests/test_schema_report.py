"""The CLI reports real stored DDL; the carry changes only a new copy."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from agent_comms.cli import main
from agent_comms.cli_commands import SchemaReportCliCommand
from agent_comms.schema_report import StoreSchemaReport
from agent_comms.todos import Todo
from agent_comms.typed_table import SQLiteSchemaObject, SQLiteSchemaDifference
from test_private_bus_checkpoint import _root
from tools.cutover.carry_declared_checks import OriginalDeclaration, carry


def _old_todo(path):
    target = Todo.schema_objects()[Todo.declared_name]
    # #762 adds constraints from selectors; the existing explicit CHECK stays.
    original = target
    for item in Todo._fields():
        for constraint in item.storage.constraints(item.name, item.annotation):
            original = original.replace(f" CHECK ({constraint})", "", 1)
    assert original != target
    with closing(sqlite3.connect(path)) as db:
        db.execute(original)
        db.execute("INSERT INTO todo (id,repo,text,creator,creator_created,state,revision) "
                   "VALUES ('one','team/repo','unfinished','Alice',17002,'open',1)")
        db.commit()
    return OriginalDeclaration({Todo.declared_name: original}, {
        Todo.declared_name: {item.name: () for item in Todo._fields()}})


def test_comparison_names_changed_missing_and_extra_objects():
    assert SQLiteSchemaObject.differences(
        [SQLiteSchemaObject("changed", "old"), SQLiteSchemaObject("extra", "sql")],
        {"changed": "new", "missing": "sql"}) == (
            SQLiteSchemaDifference("changed", "old", "new"),
            SQLiteSchemaDifference("extra", "sql", None),
            SQLiteSchemaDifference("missing", None, "sql"))


def test_cli_inspects_existing_stores_without_rewriting_them(tmp_path, capsys, monkeypatch):
    # A disposable bus is not this agent's private owner-launch root.
    from agent_comms.private_nk_entrypoint import ROOT_ID_ENV, PACKAGE_ENV
    monkeypatch.delenv(ROOT_ID_ENV, raising=False)
    monkeypatch.delenv(PACKAGE_ENV, raising=False)
    comms, _ = _root(tmp_path)
    path = comms.root / "todos.sqlite3"
    _old_todo(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = SchemaReportCliCommand().apply(comms)
    todo = next(row for row in result if row["path"] == str(path))
    assert [change["name"] for change in todo["differences"]] == [Todo.declared_name]
    assert main(["--root", str(comms.root), "schema-report"]) == 0
    assert '"differences"' in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_unknown_and_unavailable_stores_do_not_look_clean(tmp_path):
    comms, _ = _root(tmp_path)
    with closing(sqlite3.connect(comms.root / "unknown.sqlite3")) as db:
        db.execute("CREATE TABLE unowned (x)")
    (comms.root / "broken.sqlite3").write_bytes(b"not a database")
    reports = SchemaReportCliCommand().apply(comms)
    assert next(row for row in reports if row["path"].endswith("unknown.sqlite3"))["undeclared_tables"] == ["unowned"]
    assert next(row for row in reports if row["path"].endswith("broken.sqlite3"))["error"]


def test_carry_preserves_rows_and_original_and_refuses_overwrite(tmp_path):
    source = tmp_path / "old.sqlite3"
    original = _old_todo(source)
    before = source.read_bytes()
    target = tmp_path / "carried.sqlite3"
    result = carry(source, target, original)
    assert result["carried"] == [Todo.declared_name]
    assert source.read_bytes() == before
    assert StoreSchemaReport.observe(target).differences == ()
    with closing(sqlite3.connect(target)) as db:
        assert db.execute("SELECT text FROM todo WHERE id='one'").fetchone() == ("unfinished",)
    with pytest.raises(FileExistsError):
        carry(source, target, original)


def test_carry_refuses_wrong_preimage_and_rows_that_violate_new_checks(tmp_path):
    source = tmp_path / "old.sqlite3"
    original = _old_todo(source)
    with pytest.raises(ValueError, match="authentic original"):
        carry(source, tmp_path / "wrong.sqlite3", OriginalDeclaration(
            {Todo.declared_name: "wrong"}, original.constraints))
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE todo SET state='invalid'")
        db.commit()
    before = source.read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        carry(source, tmp_path / "invalid.sqlite3", original)
    assert source.read_bytes() == before
    assert not (tmp_path / "invalid.sqlite3").exists()
