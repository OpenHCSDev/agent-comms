"""Current source coverage must stay with its proof and admission owners."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"
RETIRED = {
    "advance_current_native_cursor",
    "read_current_native_cursor",
    "read_proven_source_coverage",
    "_bounded_coverage_pages",
    "_prefix_evidence",
    "_same_generation_prefix",
    "_last_source_proof",
    "_source_witness",
    "_source_witness_unlocked",
}


def test_no_retired_cursor_procedures_or_callers():
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Name, ast.alias)):
                name = node.id if isinstance(node, ast.Name) else node.name
                assert name not in RETIRED, (path, name)


def test_source_proof_owners_stay_within_s7_bounds():
    for name in (
        "native_source_cursor",
        "cursor_owner",
        "proven_source_coverage",
        "source_proof_requirement",
    ):
        source = (SOURCE / f"{name}.py").read_text()
        assert len(source.splitlines()) <= 1000
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (name, node.name)
    reader = ast.parse((SOURCE / "proven_source_coverage.py").read_text())
    assert not any(isinstance(n, ast.Attribute) and n.attr == "triage" for n in ast.walk(reader))
