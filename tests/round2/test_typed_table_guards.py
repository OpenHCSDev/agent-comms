"""A13 owns all SQLite DDL, mutation columns and typed reads package-wide."""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard

SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def violations(tree):
    failures = []
    cursors = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value.func
            if isinstance(call, ast.Attribute) and call.attr == "execute":
                cursors.update(target.id for target in node.targets if isinstance(target, ast.Name))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(
                r"\b(CREATE\s+TABLE|INSERT\s+INTO|UPDATE\s+\w+\s+SET)\b", node.value, re.I
            )
        ):
            failures.append((node.lineno, "hand-written table DDL or write"))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"fetchone", "fetchall", "fetchmany"}
        ):
            failures.append((node.lineno, "raw SQLite row extraction"))
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in cursors
        ):
            failures.append((node.lineno, "raw SQLite cursor indexing"))
        if isinstance(node, (ast.For, ast.comprehension)):
            iterator = node.iter
            if (
                isinstance(iterator, ast.Name)
                and iterator.id in cursors
                or isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Attribute)
                and iterator.func.attr == "execute"
            ):
                failures.append((iterator.lineno, "raw SQLite row iteration"))
    return failures


def test_production_sqlite_access_uses_the_declared_boundary():
    from agent_comms import typed_table

    owner = SOURCE / Path(typed_table.__file__).name
    failures = {
        str(path.relative_to(SOURCE)): violations(ast.parse(path.read_text()))
        for path in SOURCE.rglob("*.py")
        if path.resolve() != owner
    }
    assert not {name: hits for name, hits in failures.items() if hits}


def test_guard_rejects_reintroduced_mechanisms():
    for source in (
        'db.execute("CREATE TABLE rows (value TEXT)")',
        'db.execute("INSERT INTO rows VALUES (?)", (value,))',
        'db.execute("UPDATE rows SET value=?", (value,))',
        'db.execute("SELECT value FROM rows").fetchone()[0]',
        'cursor = db.execute(query)\nvalue = cursor["value"]',
        'cursor = db.execute(query)\nvalues = [row["value"] for row in cursor]',
    ):
        assert violations(ast.parse(source)), source
    assert not violations(ast.parse("Row.read(db.execute(query, parameters))"))
