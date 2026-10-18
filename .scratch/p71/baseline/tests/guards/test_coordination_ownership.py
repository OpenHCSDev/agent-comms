"""Coordinator declarations cannot return to an aggregate or acquire a second registry."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.typed_table import Column, TypedTable

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_deleted_coordinator_aggregate_has_no_callers():
    assert not (SOURCE / "coordination.py").exists()
    violations = []
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                if node.module in {"coordination", "agent_comms.coordination"} or (
                    node.module in {None, "agent_comms"}
                    and any(item.name == "coordination" for item in node.names)
                ):
                    violations.append((path, node.lineno))
            elif isinstance(node, ast.Import) and any(
                item.name == "agent_comms.coordination" for item in node.names
            ):
                violations.append((path, node.lineno))
    assert not violations


def test_new_declared_table_is_installed_without_schema_roster_edit(tmp_path):
    @dataclass(frozen=True)
    class ProofRow(CoordinatorTable, TypedTable, declared_name="coordinator_extension_probe"):
        identity: str = field(metadata={"sql": Column(primary_key=True)})
        value: int

    try:
        database = tmp_path / "coordination.sqlite3"
        with CoordinationStore(database) as store:
            row = ProofRow("new-declaration", 7)
            row.insert(store._connection)
            assert ProofRow.one(store._connection, identity=row.identity) == row
        with CoordinationStore(database) as reopened:
            assert ProofRow.one(reopened._connection, identity=row.identity) == row
    finally:
        TypedTable.__registry__.pop(ProofRow.declared_name)


def test_coordinator_row_family_stays_within_original_s7_module_and_method_limits():
    import inspect

    modules = {Path(inspect.getfile(row)) for row in TypedTable.members_with(CoordinatorTable)}
    modules.update((SOURCE / "coordination_database.py", SOURCE / "coordination_schema.py"))
    for path in modules:
        source = path.read_text()
        assert len(source.splitlines()) <= 1000, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (path, node.name)
