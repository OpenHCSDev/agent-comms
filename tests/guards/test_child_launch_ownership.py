"""Tools and tests launch children through child_process, which binds their lifetime.

A raw launch has no parent-death binding: a killed pytest worker leaves it running
under init (2026-10-21: about 120 debt ratchet processes, 15 hours old). The count of
raw launches may only fall; lower RAW_LAUNCHES when one moves to the owner.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard

ROOT = Path(__file__).resolve().parents[2]
RAW_LAUNCHES = 32


class RawLaunch(ast.NodeVisitor):
    """Calls that start a process without SpawnedProcess, AttachedChild or ForkedProcessPool."""

    owners = {"subprocess": {"Popen"}, "multiprocessing": {"Pool", "Process"},
              "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
              "os": {"fork", "forkpty"}}
    names = {"Popen", "ProcessPoolExecutor", "create_subprocess_exec", "create_subprocess_shell"}

    def __init__(self) -> None:
        self.found: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            raw = function.attr in self.owners.get(function.value.id, ()) or function.attr in self.names
        else:
            raw = isinstance(function, ast.Name) and function.id in self.names
        if raw:
            self.found.append(node.lineno)
        self.generic_visit(node)


def raw_launches() -> dict[str, list[int]]:
    found = {}
    for path in sorted((*ROOT.glob("tools/**/*.py"), *ROOT.glob("tests/**/*.py"))):
        visitor = RawLaunch()
        visitor.visit(ast.parse(path.read_bytes(), str(path)))
        if visitor.found:
            found[str(path.relative_to(ROOT))] = visitor.found
    return found


def test_raw_child_launches_in_tools_and_tests_only_fall():
    found = raw_launches()
    count = sum(map(len, found.values()))
    assert count <= RAW_LAUNCHES, f"new raw launch; use agent_comms.child_process: {found}"
    assert count == RAW_LAUNCHES, f"{count} raw launches remain; lower RAW_LAUNCHES to it"
