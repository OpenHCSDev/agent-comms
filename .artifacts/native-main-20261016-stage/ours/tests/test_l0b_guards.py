"""Retired root switching and public writer APIs cannot return during integration."""

import ast
import re
from pathlib import Path

import pytest


@pytest.mark.refactor_guard
def test_current_wire_owners_have_no_retired_root_switching():
    root = Path(__file__).parents[1] / "src" / "agent_comms"
    assert not list(root.glob("*cutover*.py"))
    forbidden = re.compile(r"legacy|compat|deprecat|backward|fallback|shim", re.IGNORECASE)
    for name in ("active_route.py", "publisher.py", "wire_log.py"):
        source = (root / name).read_text()
        assert forbidden.search(source) is None, name
        functions = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not functions.intersection(
            {
                "rotate_active_route",
                "withdraw_active_route",
                "publish",
                "remove_legacy_threads",
                "assert_legacy_rewrite_allowed",
            }
        ), name
