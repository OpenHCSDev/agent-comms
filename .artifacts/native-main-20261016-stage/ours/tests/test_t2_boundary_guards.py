"""Prevent rebuilding the deleted key protocol beside the shared declarations."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "agent_comms"


def test_extension_is_only_declared_at_boundary():
    for path in ROOT.glob("*.py"):
        if path.name == "acp_extension.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "agentComms":
                raise AssertionError(f"Undeclared extension access in {path.name}:{node.lineno}")


def test_retired_attachment_protocol_is_deleted():
    paths = (
        "acp.py",
        "input_drain.py",
        "native_source_cursor.py",
        "runtime.py",
        "session_lifecycle.py",
    )
    for name in paths:
        source = (ROOT / name).read_text()
        for retired in (
            "ownerEpoch",
            "turnLifecycle",
            "promptQueue",
            "imagePrompts",
            "autoTitle",
            "_present_cursor_session",
        ):
            assert retired not in source, (name, retired)
        assert "epoch" not in source, name
