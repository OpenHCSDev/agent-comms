"""C1: new Pi/import cases change their declaration, never consumer switches."""

import ast
from pathlib import Path

import pytest

from agent_comms.pi_summary_payloads import SelectedSummaryData
from agent_comms.pi_vocabulary import CompactionReason, PiStopReason, ThinkingLevel

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_pi_spellings_are_not_compared_by_consumers():
    """Reject both an entire roster and the first new single-case bypass."""
    violations = []
    for path in SOURCE.glob("*.py"):
        if path.name == "pi_vocabulary.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Compare):
                continue
            subjects = [node.left, *node.comparators]
            has_vocabulary = any(
                isinstance(part, ast.Attribute)
                and part.attr in {"stop_reason", "thinking_level", "selected_thinking_level"}
                for part in subjects
            )
            has_spelling = any(
                isinstance(part, ast.Constant) and isinstance(part.value, str)
                for subject in subjects
                for part in ast.walk(subject)
            )
            if has_vocabulary and has_spelling:
                violations.append(f"{path.name}:{node.lineno}")
    assert not violations, violations


def test_summary_members_own_the_response_contract():
    assert SelectedSummaryData.__abstractmethods__ == frozenset({"response"})
    members = SelectedSummaryData.members_with(SelectedSummaryData)
    assert members and all("response" in vars(member) for member in members)
    source = ast.parse((SOURCE / "selected_pi_summary_rpc.py").read_text())
    decoder = next(
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "_summary_response"
    )
    assert not any(
        isinstance(node, ast.Name) and node.id.startswith("Summary") for body in decoder.body for node in ast.walk(body)
    )


def test_vocabularies_derive_names_from_their_declarations():
    # These are external native spellings, not a second production registry.
    assert set(PiStopReason.names()) - {"unreported"} == {
        "pending",
        "stop",
        "length",
        "toolUse",
        "error",
        "aborted",
        "deferred",
    }
    assert CompactionReason.names() == ("manual", "overflow", "threshold", "unknown")
    assert ThinkingLevel.selected_names() == ("low", "high")
