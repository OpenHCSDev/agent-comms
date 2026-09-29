"""Mutation owners keep their state; the removed aggregate cannot return."""

import ast
import inspect
from pathlib import Path

import pytest

from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.attempt_start import AttemptStart
from agent_comms.coordinator import Coordination

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_no_mutation_aggregate_or_compatibility_dispatch():
    assert not (SOURCE / "coordination_store.py").exists()
    failures = []
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and node.id == "MutationStore":
                failures.append((path, node.lineno))
            if isinstance(node, ast.ClassDef) and node.name == "MutationStore":
                failures.append((path, node.lineno))
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module in {"coordination_store", "agent_comms.coordination_store"}
                    or any(
                        alias.name
                        in {"MutationStore", "agent_comms.coordination_store", "coordination_store"}
                        for alias in node.names
                    )
                ):
                    failures.append((path, node.lineno))
    assert not failures
    root = ast.parse(inspect.getsource(Coordination)).body[0]
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name in {"__getattr__", "__getattribute__"}
        for node in root.body
    )


def test_owned_modules_and_methods_obey_s7_limits(tmp_path):
    with Coordination(tmp_path / "coordination.sqlite3") as coordination:
        owners = {type(value) for value in vars(coordination).values()}
    owners.update((Coordination, AttemptStart, RecoveryMonitorCapability))
    for path in {Path(inspect.getfile(owner)) for owner in owners}:
        source = path.read_text()
        assert len(source.splitlines()) <= 1000, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (path, node.name)
