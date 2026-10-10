"""AST gates: Core reaches the Pi runtime only through its AgentBackend.

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
    ("terminate_task_process", {"backend.py", "native_session_prepare.py", "pi_native_backend.py"}),
    # A session's Pi child is created only by its backend, or by a foreground
    # execution that owns its one child (default_factory, not a call).
    ("PersistentPiSession", set()),
    ("PiNativeBackend", set()),
])
def test_runtime_entry_points_have_one_owner(name, owners):
    callers = {module for module, tree in _modules().items() if name in _called_names(tree)}
    assert callers == owners


@pytest.mark.refactor_guard
def test_no_consumer_creates_a_backend_except_from_the_thread_declaration():
    for module, tree in _modules().items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "persistent_backends"
            ):
                pytest.fail(f"{module}:{node.lineno} creates a backend outside backend_for")


@pytest.mark.refactor_guard
def test_turn_inbox_carries_core_records_not_pi_wire():
    """No Core module spells the Pi wire of a queued input or a send-now."""
    spelled = []
    for module, tree in _modules().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in {
                "_input_id", "_input_ids", "interrupt_steering",
            }:
                spelled.append(f"{module}:{node.lineno}:{node.value}")
    assert spelled == []


@pytest.mark.refactor_guard
def test_pi_launch_arguments_belong_to_the_backend():
    trees = _modules()
    runner = next(
        node for node in ast.walk(trees["turn_runner.py"])
        if isinstance(node, ast.ClassDef) and node.name == "TurnRunner"
    )
    methods = {node.name for node in runner.body if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)}
    assert "native_arguments" not in methods
    assert "backend_for" in methods
