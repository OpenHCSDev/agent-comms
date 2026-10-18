"""Current source must use defining modules after the aggregate is removed."""

import ast
from pathlib import Path


def test_no_retired_declaration_module_or_current_imports():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "src/agent_comms/declarations.py").exists()
    for folder in ("src", "tests", "scripts", "tools", "benchmarks", "stack"):
        for source in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert node.module not in {"declarations", "agent_comms.declarations"}, source
                    if node.module == "agent_comms":
                        assert all(name.name != "declarations" for name in node.names), source
                if isinstance(node, ast.Import):
                    assert all(name.name != "agent_comms.declarations" for name in node.names), (
                        source
                    )
