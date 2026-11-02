"""AST gate: Core reaches the Pi runtime only through its AgentBackend.

Source evidence only (dynamic attribute access is not resolved); every module
under src/agent_comms must parse, or the gate fails.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_comms"


@cache
def _modules() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(SOURCE)): ast.parse(path.read_text(), str(path))
        for path in SOURCE.rglob("*.py")
    }


def _called_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", None))
    return names


@pytest.mark.refactor_guard
@pytest.mark.parametrize(("name", "owners"), [
    # The Pi turn transport is the backend's private implementation.
    ("stream_agent_events", {"pi_native_backend.py"}),
    ("terminate_task_process", {"pi_native_backend.py"}),
    # A session's Pi child is created only by its backend, or by a foreground
    # execution that owns its one child (default_factory, not a call).
    ("PersistentPiSession", set()),
    ("PiNativeBackend", set()),
])
def test_runtime_entry_points_have_one_owner(name, owners):
    callers = {module for module, tree in _modules().items() if name in _called_names(tree)}
    assert callers == owners
