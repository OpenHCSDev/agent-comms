"""Transcript routing lookups stay bounded as saved history grows."""

from conftest import transcript_records
import json
from pathlib import Path

from agent_comms.routing import MessageRoute, TurnRouting


def _session(path: Path, count: int) -> None:
    with path.open("w") as output:
        for index in range(count):
            output.write(
                json.dumps(
                    transcript_records.message({
                            "role": "assistant",
                            "content": [{"type": "text", "text": f"answer {index}"}],
                        }, id=f"entry-{index}")
                )
                + "\n"
            )


def test_current_annotations_reopen_and_replace_only_addressed_entry(tmp_path):
    from agent_comms.transcript_routes import TranscriptRoutes

    path = tmp_path / "annotations"
    owner = TranscriptRoutes(path)
    with owner.for_session("session") as routes:
        assert routes.get("a") is None
    assert not path.exists()
    first = TurnRouting(reply=MessageRoute("worker", ("#first",)))
    replacement = TurnRouting(reply=MessageRoute("worker", ("#replacement",)))
    owner.record("session", ("a", "b"), first)
    TranscriptRoutes(path).record("session", ("a",), replacement)
    with TranscriptRoutes(path).for_session("session") as routes:
        assert routes.get("a") == replacement
        assert routes.get("b") == first
        assert routes.get("absent") is None


def test_binding_preserves_first_display_and_rejects_different_input(tmp_path):
    import pytest

    from agent_comms.errors import RelationViolationError
    from agent_comms.transcript_routes import TranscriptRoutes

    owner = TranscriptRoutes(tmp_path)
    owner.record_input_display("input", "human display")
    owner.record_input_display("input", "wrapped display", sent_text="exact sent text")
    with pytest.raises(RelationViolationError):
        owner.record_input_display("input", "overwrite", sent_text="different text")
    with TranscriptRoutes(tmp_path).for_session("session") as routes:
        row = routes.input_display("input")
        assert row.text == "human display"
        assert row.matches("exact sent text")
        assert not row.matches("different text")
