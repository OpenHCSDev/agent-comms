"""Native source observation has one checked reader and bootstrap phase."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_source_coverage_has_no_whole_bus_reader_or_hashed_tuple():
    source = (SOURCE / "proven_source_coverage.py").read_text()
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr in {"verified_records_unlocked", "sha256", "read_bytes"}
        for node in ast.walk(tree)
    )
    assert "_MAX_SOURCE_BYTES" not in source
    assert "_MAX_BUS_BYTES" not in source
    assert "_MAX_BUS_ROWS" not in source
    assert "PrefixWitness | tuple" not in source


