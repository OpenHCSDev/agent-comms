"""S9 deletion contracts, not a per-file debt allowance."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
ROOT = Path(__file__).resolve().parents[2] / "src/agent_comms"


def files():
    return sorted(
        set(ROOT.glob("owner_compaction_*.py"))
        | {
            ROOT / name
            for name in (
                "manual_compaction_bridge.py",
                "native_session_reopen.py",
                "compaction_journal.py",
                "compaction_states.py",
                "selected_summary_admission.py",
                "pi_helper.py",
                "fresh_private_session.py",
                "continued_private_session.py",
            )
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


def test_removed_mechanisms_and_settings_have_one_owner():
    for name in (
        "selected_source_snapshot.py",
        "compaction_child_launcher.py",
        "owner_compaction_process.py",
        "compaction_child_watchdog.py",
    ):
        assert not (ROOT / name).exists(), name
    for path in files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name):
                assert node.id != "reservable_commit", (path.name, node.lineno)
            elif isinstance(node, ast.Attribute):
                assert node.attr != "reservable_commit", (path.name, node.lineno)
    declarations = []
    for path in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                declared = {
                    child.target.id
                    for child in node.body
                    if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
                }
                if {"reserve_tokens", "keep_recent_tokens"} <= declared:
                    declarations.append((path.name, node.name))
    assert declarations == [("owner_compaction_settings.py", "PiCompactionSettings")]
