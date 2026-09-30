"""Native-only managed execution cannot regain a text engine or polling owner."""

import ast
from pathlib import Path

import pytest

from agent_comms import backend

pytestmark = pytest.mark.refactor_guard


def test_managed_execution_has_one_native_owner():
    root = Path(backend.__file__).parent
    assert not (root / "agent_loop.py").exists()
    assert not (root / "passive_channel_awareness.py").exists()
    assert not (root / "manual_compaction.py").exists()
    assert not (root / "_pi_helpers/manual_preflight.mjs").exists()
    for name in (
        "backend.py",
        "input_drain.py",
        "owned_turn.py",
        "turn_runner.py",
        "acp.py",
        "session_lifecycle.py",
        "channel_management.py",
        "thread_management.py",
    ):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {
                    "passive_awareness",
                    "passive_frame",
                    "passive_sources",
                    "_rebase_passive_channel_scope",
                }
            if isinstance(node, (ast.Name, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert getattr(node, "id", getattr(node, "name", None)) != "rpc_args_for"
            if isinstance(node, ast.ImportFrom):
                assert all(alias.name != "rpc_args_for" for alias in node.names)
    tree = ast.parse((root / "backend.py").read_text())
    session = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TurnSession")
    for node in ast.walk(session):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"agent_bin", "agent_args", "rpc_args", "argv"} or (
                node.attr == "argv" and ast.unparse(node.value) == "self.launch"
            )
        if isinstance(node, ast.Call):
            assert ast.unparse(node.func) != "self.proc.stdout.read"
    project = root.parents[1] / "pyproject.toml"
    assert 'agent-comms-agent = "agent_comms.worker:main"' in project.read_text()
    worker = ast.parse((root / 'worker.py').read_text())
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {'declare', 'register'}
        for node in ast.walk(worker)
    ), 'The headless launcher must consume original owner registration, not mint one'
