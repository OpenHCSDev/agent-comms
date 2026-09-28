"""A12 guards; consumer-wide guard is completed with S13 caller migration."""

from __future__ import annotations

import ast
from pathlib import Path

from agent_comms import child_process


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
        options = {keyword.arg: keyword.value for keyword in call.keywords}
        assert isinstance(options.get("start_new_session"), ast.Constant)
        assert options["start_new_session"].value is True


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
