"""The CLI reports stored declarations without changing existing databases."""
from contextlib import closing
import sqlite3

from agent_comms.cli_commands import SchemaReportCliCommand
from agent_comms.typed_table import SQLiteSchemaObject, SQLiteSchemaDifference
from test_private_bus_checkpoint import _root


def test_comparison_names_changed_missing_and_extra_objects():
    assert SQLiteSchemaObject.differences(
        [SQLiteSchemaObject("changed", "old"), SQLiteSchemaObject("extra", "sql")],
        {"changed": "new", "missing": "sql"}) == (
            SQLiteSchemaDifference("changed", "old", "new"),
            SQLiteSchemaDifference("extra", "sql", None),
            SQLiteSchemaDifference("missing", None, "sql"))


def test_unknown_and_unavailable_stores_do_not_look_clean(tmp_path):
    comms, _ = _root(tmp_path)
    with closing(sqlite3.connect(comms.root / "unknown.sqlite3")) as db:
        db.execute("CREATE TABLE unowned (x)")
    (comms.root / "broken.sqlite3").write_bytes(b"not a database")
    reports = SchemaReportCliCommand().apply(comms)
    assert next(row for row in reports if row["path"].endswith("unknown.sqlite3"))["undeclared_tables"] == ["unowned"]
    assert next(row for row in reports if row["path"].endswith("broken.sqlite3"))["error"]
