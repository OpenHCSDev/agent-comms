"""Keep the fresh source's replaced anonymous decisions deleted."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_fresh_source_has_no_long_validation_chain_or_split_identity():
    # Original S14 witnesses were the saved/opened/startup conditions here.
    # Across all declarations, including future rules, no copied chain returns.
    tree = ast.parse((SOURCE / "fresh_private_session.py").read_text())
    assert not [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.BoolOp) and len(node.values) >= 6]
    enrolled = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "FreshPrivateSession")
    fields = {node.target.id for node in enrolled.body
              if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    assert "file_identity" in fields and not fields & {"device", "inode"}
    checks = [node for node in tree.body
              if isinstance(node, ast.ClassDef) and node.name.endswith("FileCheck")]
    assert not [field.lineno for check in checks for field in check.body
                if isinstance(field, ast.AnnAssign)
                and any(isinstance(part, ast.Constant) and part.value is None
                        for part in ast.walk(field.annotation))]
