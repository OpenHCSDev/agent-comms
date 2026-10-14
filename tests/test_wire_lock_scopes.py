"""A wire-lock body must never suspend its event loop while holding custody."""

import ast
from pathlib import Path


SOURCE_ROOT = Path("src/agent_comms")


def _called_name(expression: ast.expr) -> str:
    if not isinstance(expression, ast.Call):
        return ""
    function = expression.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def test_no_async_wire_lock_scope_contains_await():
    violations = []
    parsed = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        parsed.append(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            if not any(_called_name(item.context_expr) == "_async_store_lock"
                       for item in node.items):
                continue
            awaits = tuple(child.lineno for child in ast.walk(node)
                           if isinstance(child, ast.Await))
            if awaits:
                violations.append((str(path), node.lineno, awaits))
    assert parsed
    assert not violations, f"wire lock held across await: {violations}"
