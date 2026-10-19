"""AST gates: the time an input was shown to the model is read from its row.

The native writer stamps committedAtMs on the context journal row and the live
event; Core stores it on native_runtime_input. Notification and presentation
reads never decode native session files to recover it.

Source evidence only (dynamic attribute access is not resolved); every module
under src/agent_comms must parse, or the gate fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_comms"


def _modules() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(SOURCE)): ast.parse(path.read_text(), str(path))
        for path in SOURCE.rglob("*.py")
    }


def _callers(name: str) -> set[str]:
    callers = set()
    for module, tree in _modules().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) == name:
                    callers.add(module)
    return callers


@pytest.mark.refactor_guard
@pytest.mark.parametrize(("name", "owners"), [
    # Proof, recovery and transcript owners corroborate native evidence; no
    # notification, presentation or observation module opens it.
    ("open_evidence", {"attempt_recovery.py", "native_entries.py", "native_pi.py", "native_transcript.py"}),
    ("open_input_evidence", set()),
    ("read_evidence", {"attempt_recovery.py", "native_pi.py"}),
])
def test_native_evidence_has_fixed_readers(name, owners):
    assert _callers(name) == owners


@pytest.mark.refactor_guard
@pytest.mark.parametrize("name", [
    "recorded_times", "recorded_progress", "AssignmentContext", "RemovedContextProgress", "open_input_evidence",
])
def test_rederived_read_time_is_deleted(name):
    for module, tree in _modules().items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
                pytest.fail(f"{module}:{node.lineno} declares {name}")
            if isinstance(node, (ast.Name, ast.Attribute)) and getattr(
                node, "id", getattr(node, "attr", None)
            ) == name:
                pytest.fail(f"{module}:{node.lineno} uses {name}")
