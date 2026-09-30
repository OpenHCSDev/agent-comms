"""Current source coverage must stay with its proof and admission owners."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"
RETIRED = {
    "advance_current_native_cursor",
    "read_current_native_cursor",
    "read_proven_source_coverage",
    "_bounded_coverage_pages",
    "_prefix_evidence",
    "_same_generation_prefix",
    "_last_source_proof",
    "_source_witness",
    "_source_witness_unlocked",
    "sent_tool_message",
    "AgentTextTranscriptUpdate",
    "_private_cursor_scope",
    "_private_cursor_metadata",
    "_publish_private_cursor",
    "_refresh_private_cursor",
    "_session_runtime_metadata",
    "_private_cursor_announced",
    "_private_cursor_revisions",
    "_receipt_offsets",
}


def test_no_retired_cursor_procedures_or_callers():
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            match node:
                case (
                    ast.ClassDef(name=name)
                    | ast.FunctionDef(name=name)
                    | ast.AsyncFunctionDef(name=name)
                ):
                    assert name not in RETIRED, (path, name)
                case ast.Name(id=name) | ast.alias(name=name) | ast.Attribute(attr=name):
                    assert name not in RETIRED, (path, name)


def test_source_proof_owners_stay_within_s7_bounds():
    for name in (
        "native_source_cursor",
        "cursor_owner",
        "proven_source_coverage",
        "source_proof_requirement",
        "cursor_publication",
    ):
        source = (SOURCE / f"{name}.py").read_text()
        assert len(source.splitlines()) <= 1000
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (name, node.name)
    reader = ast.parse((SOURCE / "proven_source_coverage.py").read_text())
    assert not any(isinstance(n, ast.Attribute) and n.attr == "triage" for n in ast.walk(reader))


def test_outbound_transcript_cannot_be_recreated_from_a_tool_result():
    """History ad9bde97 added live and saved synthetic send append sites."""
    for filename, retired in (
        ("turn_progress.py", "AgentTextTranscriptUpdate"),
        ("pi_payloads.py", "SentTranscript"),
    ):
        assert not any(
            isinstance(node, ast.Name) and node.id == retired
            for node in ast.walk(ast.parse((SOURCE / filename).read_text()))
        )


def test_original_wire_reply_has_no_native_streaming_membership():
    """Real215-05 and composition215-01 rendered one row through both handlers."""
    from agent_comms.transcript_events import (
        AgentTextTranscript, IncomingTranscript, SentTranscript, UserTranscript,
        MarkdownTranscript, WireTextTranscript,
    )
    from agent_comms.transcript_merge import StreamingMerge

    assert not issubclass(SentTranscript, AgentTextTranscript)
    assert not issubclass(SentTranscript, StreamingMerge)
    assert not issubclass(IncomingTranscript, UserTranscript)
    assert not issubclass(IncomingTranscript, StreamingMerge)
    assert issubclass(WireTextTranscript, MarkdownTranscript)
