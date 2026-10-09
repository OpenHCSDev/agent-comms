"""Transcript routing lookups stay bounded as saved history grows."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.routing import MessageRoute, TurnRouting
from agent_comms.threads import Thread
from agent_comms.transcript_routes import TranscriptRoute


def _session(path: Path, count: int) -> None:
    with path.open("w") as output:
        for index in range(count):
            output.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": f"entry-{index}",
                        "message": {"role": "assistant", "content": f"answer {index}"},
                    }
                )
                + "\n"
            )


def test_tail_page_decodes_only_its_routing_entries(tmp_path, monkeypatch) -> None:
    session = tmp_path / "session.jsonl"
    _session(session, 10_000)
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    routing = TurnRouting(reply=MessageRoute("worker", ("#team",)))
    owner = comms.transcripts.routes
    owner._ensure_database(create=True)
    with closing(sqlite3.connect(owner.database_path)) as connection, connection:
        for index in range(10_000):
            TranscriptRoute(str(session), f"entry-{index}", routing).insert(connection)

    decoded = 0
    real_decode = FieldCodec.decode

    def counted_decode(target, payload):
        nonlocal decoded
        decoded += int(target is TurnRouting)
        return real_decode(target, payload)

    monkeypatch.setattr(FieldCodec, "decode", staticmethod(counted_decode))
    page = wire(tmp_path / "wire").transcripts.thread_transcript_page("worker")
    assert [event.text for event in page.events] == [
        f"answer {index}" for index in range(9980, 10_000)
    ]
    assert all(event.routing == routing for event in page.events)
    assert decoded <= 25



def test_current_annotations_reopen_and_replace_only_addressed_entry(tmp_path):
    from agent_comms.transcript_routes import TranscriptRoutes

    path = tmp_path / "annotations"
    owner = TranscriptRoutes(path)
    with owner.for_session("session") as routes:
        assert routes.get("a") is None
    assert not path.exists()
    first = TurnRouting(reply=MessageRoute("worker", ("#first",)))
    replacement = TurnRouting(reply=MessageRoute("worker", ("#replacement",)))
    owner._ensure_database(create=True)
    with closing(sqlite3.connect(owner.database_path)) as connection, connection:
        for identity in ("a", "b"):
            TranscriptRoute("session", identity, first).insert(connection)
        TranscriptRoute("session", "a", replacement).upsert(connection)
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
    with pytest.raises(RelationViolationError, match="cannot be rebound"):
        owner.record_input_display("input", "overwrite", sent_text="different text")
    with TranscriptRoutes(tmp_path).for_session("session") as routes:
        row = routes.input_display("input")
        assert row.text == "human display"
        assert row.matches("exact sent text")
        assert not row.matches("different text")
