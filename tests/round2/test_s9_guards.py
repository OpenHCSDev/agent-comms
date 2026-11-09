"""S9 deletion contracts, not a per-file debt allowance."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
from agent_comms.execution import fresh_private_session, manual_compaction_bridge, native_session_reopen, pi_helper

ROOT = Path(__file__).resolve().parents[2] / "src/agent_comms"


def files():
    """The compaction modules wherever their package puts them, plus the named helpers found
    through their imports, so a move never shrinks what the guard reads."""
    return sorted(
        set(ROOT.rglob("owner_compaction_*.py"))
        | set(ROOT.rglob("compaction_*.py"))
        | set(ROOT.rglob("native_compaction_*.py"))
        | {
            Path(module.__file__)
            for module in (manual_compaction_bridge, native_session_reopen, pi_helper, fresh_private_session)
        }
    )


def test_no_local_process_supervision_or_embedded_programs():
    violations = []
    for path in files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(name == "subprocess" for name in names):
                    violations.append((path.name, node.lineno, "local process import"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {
                    "kill",
                    "killpg",
                    "Popen",
                    "create_subprocess_exec",
                    "create_subprocess_shell",
                }
            ):
                violations.append((path.name, node.lineno, "local process control"))
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "node:" in node.value
                and "import" in node.value
            ):
                violations.append((path.name, node.lineno, "embedded JS"))
    assert not violations


def test_no_exact_keysets_or_column_decoders():
    violations = []
    for path in files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops
            ):
                for part in (node.left, *node.comparators):
                    if (
                        isinstance(part, ast.Call)
                        and isinstance(part.func, ast.Name)
                        and part.func.id == "set"
                    ):
                        violations.append((path.name, node.lineno, "exact keyset"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"fetchone", "fetchall", "fetchmany", "from_row", "from_columns"}
            ):
                violations.append((path.name, node.lineno, "raw column decoder"))
    assert not violations


