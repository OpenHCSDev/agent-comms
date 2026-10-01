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
                and part.attr in {
                    "stop_reason", "compaction_reason", "thinking_level", "selected_thinking_level"
                }
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


def test_tracked_receipt_and_terminal_data_do_not_restore_nullable_slots():
    source = ast.parse((SOURCE / "tracked_turn.py").read_text())
    replaced = {"input_event", "context_event", "terminal_error", "final_messages"}
    assert not [
        f"{node.attr}:{node.lineno}"
        for node in ast.walk(source)
        if isinstance(node, ast.Attribute) and node.attr in replaced
    ]


def test_summary_members_own_the_response_contract():
    assert SelectedSummaryData.__abstractmethods__ == frozenset({"response"})
    members = SelectedSummaryData.members_with(SelectedSummaryData)
    assert members and all("response" in vars(member) for member in members)
    assert all(not hasattr(member, name) for member in members for name in ("summary", "decline_reason"))
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


def test_c1_observation_consumers_do_not_reconstruct_absence():
    """Native optional JSON is decoded before response/model/tool consumers."""
    consumers = {
        "pi_events.py": {"Response", "ToolExecutionEnd", "ToolExecutionUpdate"},
        "pi_payloads.py": {"AssistantMessage", "StateData"},
        "native_tools.py": {"EditTool"},
        "pi_commands.py": {"SessionSnapshot", "GetState", "CatalogQuery"},
        "native_attestation.py": {"PendingAttestation"},
        "turn_stats.py": {"StatsRequest"},
    }
    violations = []
    for filename, owners in consumers.items():
        for declaration in ast.parse((SOURCE / filename).read_text()).body:
            if not isinstance(declaration, ast.ClassDef) or declaration.name not in owners:
                continue
            for method in declaration.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name.startswith("normalize_"):
                    continue
                for comparison in ast.walk(method):
                    if not isinstance(comparison, ast.Compare):
                        continue
                    terms = [comparison.left, *comparison.comparators]
                    if any(isinstance(term, ast.Constant) and term.value is None for term in terms) and any(
                        isinstance(term, ast.Attribute) and term.attr in {"data", "model", "result", "partial_result", "content"}
                        for term in terms
                    ):
                        violations.append(f"{filename}:{comparison.lineno}")
    assert not violations, violations
