"""Prevent restoration of the retired channel/catalog mechanism."""

import ast
from pathlib import Path


def test_channel_catalog_has_one_current_document_and_no_audience_api():
    root = Path(__file__).resolve().parents[1] / "src/agent_comms"
    retired = {"aggregate_target", "members_for", "set_channel", "delete_channel", "source_paths"}
    for filename in (
        "channels.py",
        "catalog_document.py",
        "catalog_store.py",
        "channel_management.py",
    ):
        tree = ast.parse((root / filename).read_text())
        assert (
            not {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
            & retired
        )
        assert not any(
            isinstance(node, ast.Name) and node.id == "audiences" for node in ast.walk(tree)
        )
    store = ast.parse((root / "catalog_store.py").read_text())
    assert [node.name for node in store.body if isinstance(node, ast.ClassDef)] == [
        "ChannelCatalog"
    ]
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name == "_decode" for node in ast.walk(store)
    )
    for filename in ("catalog_store.py", "channels.py", "catalog_document.py"):
        text = (root / filename).read_text()
        assert not any(
            word in text.lower() for word in ("legacy", "compat", "deprecated", "fallback", "shim")
        )
    tools = ast.parse((root / "tools.py").read_text())
    assert not {"comms_set_channel", "comms_delete_channel"} & {
        node.value
        for node in ast.walk(tools)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
