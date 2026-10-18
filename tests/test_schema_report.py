"""The CLI reports stored declarations without changing existing databases."""
from contextlib import closing
import hashlib
import sqlite3

from agent_comms.cli import main
from agent_comms.cli_commands import SchemaReportCliCommand
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import Todo
from agent_comms.typed_table import SQLiteSchemaObject, SQLiteSchemaDifference
from test_private_bus_checkpoint import _root



def _old_todo(path):
    """A store created before the selector CHECKs were declared."""
    target = Todo.schema_objects()[Todo.declared_name]
    original = target
    for item in Todo._fields():
        for constraint in item.storage.family_constraints(item.name, item.annotation):
            original = original.replace(f" CHECK ({constraint})", "", 1)
    assert original != target
    with closing(sqlite3.connect(path)) as db:
        db.execute(original)
        db.execute("INSERT INTO todo (id,repo,text,creator,creator_created,state,revision,\"order\",deadline,strikes,hunts,voided,released,verdicts) "
                   "VALUES ('one','team/repo','unfinished','Alice',17002,'open',1,'{}','{}','[]','[]','[]','[]','[]')")
        db.commit()


def test_comparison_names_changed_missing_and_extra_objects():
    assert SQLiteSchemaObject.differences(
        [SQLiteSchemaObject("changed", "old"), SQLiteSchemaObject("extra", "sql")],
        {"changed": "new", "missing": "sql"}) == (
            SQLiteSchemaDifference("changed", "old", "new"),
            SQLiteSchemaDifference("extra", "sql", None),
            SQLiteSchemaDifference("missing", None, "sql"))


def test_cli_inspects_existing_stores_without_rewriting_them(tmp_path, capsys, monkeypatch):
    comms, _ = _root(tmp_path)
    path = comms.root / "todos.sqlite3"
    _old_todo(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = FieldCodec.encode(SchemaReportCliCommand().apply(comms).reports)
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
    reports = FieldCodec.encode(SchemaReportCliCommand().apply(comms).reports)
    assert next(row for row in reports if row["path"].endswith("unknown.sqlite3"))["undeclared_tables"] == ["unowned"]
    assert next(row for row in reports if row["path"].endswith("broken.sqlite3"))["error"]
