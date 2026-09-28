"""A12 guards; consumer-wide guard is completed with S13 caller migration."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_comms import child_process

pytestmark = pytest.mark.refactor_guard


def test_every_spawn_has_its_own_process_group() -> None:
    tree = ast.parse(Path(child_process.__file__).read_text())
    spawns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Popen", "create_subprocess_exec"}
    ]
    assert spawns
    for call in spawns:
        assert any(
            keyword.arg is None
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == "options"
            for keyword in call.keywords
        )
    declarations = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "start_new_session" in ast.unparse(declarations["PosixLaunch"])
    assert "CREATE_NEW_PROCESS_GROUP" in ast.unparse(declarations["WindowsLaunch"])
    assert "bind_and_resume" in ast.unparse(declarations["WindowsLaunch"])


def test_detached_control_has_no_bare_pid_api() -> None:
    tree = ast.parse(Path(child_process.__file__).read_text())
    detached = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DetachedProcess"
    )
    for method in detached.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert all(argument.arg != "pid" for argument in method.args.args)
    attach = next(
        method
        for method in detached.body
        if isinstance(method, ast.FunctionDef) and method.name == "attach"
    )
    assert ast.unparse(attach.args.args[1].annotation) == "ProcessIdentity"


def test_s13_callers_cannot_reintroduce_local_supervision() -> None:
    package = Path(child_process.__file__).parent
    for name in ("backend.py", "turn_inputs.py", "owner_lifecycle.py", "recovery_gateway.py"):
        tree = ast.parse((package / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in {
                    "_terminate_process",
                    "_close_child_stdin",
                    "_process_alive",
                    "_signal_local_owner",
                    "_read_owner_release_receipts",
                }
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"create_subprocess_exec", "Popen", "killpg", "kill"}
                if isinstance(node.func.value, ast.Name):
                    assert node.func.value.id != "signal"
