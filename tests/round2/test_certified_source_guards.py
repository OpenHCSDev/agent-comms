"""Native source observation has one certified reader and bootstrap phase."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_source_coverage_has_no_whole_bus_reader_or_hashed_tuple():
    for name in ("native_source_cursor.py", "proven_source_coverage.py"):
        source = (SOURCE / name).read_text()
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


def test_obsolete_bootstrap_and_public_session_switch_are_deleted():
    for path in SOURCE.glob("*.py"):
        source = path.read_text()
        for name in (
            "initialize_private_claim_protocol",
            "enable_claim_gate_unlocked",
            "_private_session_mode",
        ):
            assert name not in source, (path.name, name)
