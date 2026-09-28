"""Permanent guards for the completed S10 boundaries."""

import ast
from pathlib import Path

import pytest

from agent_comms import pi_events, selected_tool_broker

pytestmark = pytest.mark.refactor_guard


def test_s10_selected_transport_decodes_json_directly_through_a2():
    tree = ast.parse(Path(selected_tool_broker.__file__).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert node.name not in {"from_wire", "from_arguments", "argument_names"}
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "json.loads":
            assert ast.unparse(parents[node].func) == "FieldCodec.decode"
    for declaration in tree.body:
        for node in ast.walk(declaration):
            assert not (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ), ast.unparse(node)


def test_s10_ui_choice_cannot_regain_a_raw_mapping_projection():
    tree = ast.parse(Path(pi_events.__file__).read_text())
    declaration = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExtensionUiRequest"
    )
    apply = next(node for node in declaration.body if isinstance(node, ast.AsyncFunctionDef))
    for node in (node for statement in apply.body for node in ast.walk(statement)):
        assert not isinstance(node, ast.Subscript), ast.unparse(node)
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"get", "choice", "to_wire"}, ast.unparse(node)


def test_s10_native_lifetime_is_owned_by_a12_without_retired_guardian():
    package = Path(pi_events.__file__).parent
    assert not (package / "selected_pi_child_deadline.py").exists()
    for name in ("native_pi.py", "pi_events.py", "selected_pi_summary_rpc.py"):
        tree = ast.parse((package / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in {
                    "exchange_fake_rpc",
                    "_run_fake",
                    "arm_selected_child",
                    "_pidfd_open",
                    "_pidfd_kill",
                    "_terminate_process",
                }
            if isinstance(node, ast.ImportFrom):
                assert node.module != "selected_pi_child_deadline"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "create_subprocess_exec",
                    "Popen",
                    "killpg",
                    "kill",
                    "terminate",
                }
                if isinstance(node.func.value, ast.Name):
                    assert node.func.value.id not in {"signal", "subprocess"}
