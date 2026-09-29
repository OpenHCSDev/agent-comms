"""C2 guards forbid reconstructing queue states, records or refusal behavior."""

import ast
import inspect

from agent_comms import restart_queue
from agent_comms.restart_refusals import RestartRefusal, WaitForIdle


def test_queue_uses_declared_records_environment_and_refusal_behavior():
    tree = ast.parse(inspect.getsource(restart_queue))
    policy = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == "RestartEnvironment")
    policy_nodes = {id(node) for node in ast.walk(policy)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            assert not (isinstance(node.value, ast.Name) and node.value.id in {"record", "initial", "previous"})
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "epoch" not in node.value.lower()
            if node.value.startswith(("AGENT_COMMS_", "XDG_")) or node.value in {"HOME", "PATH", "PYTHONPATH", "VIRTUAL_ENV"}:
                assert id(node) in policy_nodes
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                assert not (isinstance(operand, ast.Name) and operand.id == "reason")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert not (isinstance(node.func.value, ast.Attribute)
                        and isinstance(node.func.value.value, ast.Name)
                        and node.func.value.value.id == "os"
                        and node.func.value.attr == "environ"
                        and node.func.attr in {"clear", "update"})


def test_each_restart_refusal_declares_its_unsignalled_queue_disposition():
    for declaration in RestartRefusal.members_with(RestartRefusal):
        refusal = declaration()
        assert refusal.queue_state().pending == isinstance(refusal, WaitForIdle)
        assert str(refusal) == declaration.message
