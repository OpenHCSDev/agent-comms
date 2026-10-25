"""Transcript routing lookups stay bounded as saved history grows."""

from bus_record_fixture import INPUT_ID
from conftest import transcript_records
import json
from pathlib import Path

from agent_comms.routing import MessageRoute, TurnRouting
from agent_comms.text_digest import TextDigest
from agent_comms.turn_item import FreshNativeId
from agent_comms.transcript_routes import DisplayText, InputDisplay, NoDisplayText, SentInputText, TranscriptRoutes


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
    first_id, second_id, absent_id = (FreshNativeId.fresh() for _ in range(3))
    with owner.for_session("session") as routes:
        assert first_id.routing_in(routes).present() == ()
    assert not path.exists()
    first = TurnRouting(reply=MessageRoute("worker", ("#first",)))
    replacement = TurnRouting(reply=MessageRoute("worker", ("#replacement",)))
    owner.record("session", (first_id, second_id), first)
    TranscriptRoutes(path).record("session", (first_id,), replacement)
    with TranscriptRoutes(path).for_session("session") as routes:
        assert first_id.routing_in(routes) == replacement
        assert second_id.routing_in(routes) == first
        assert absent_id.routing_in(routes).present() == ()


def test_binding_preserves_first_display_and_rejects_different_input(tmp_path):
    import pytest

    from agent_comms.errors import RelationViolationError
    from agent_comms.transcript_routes import TranscriptRoutes

    owner = TranscriptRoutes(tmp_path)
    owner.record_input_display(INPUT_ID, DisplayText("human display"))
    owner.record_input_display(INPUT_ID, DisplayText("wrapped display"), sent_text=SentInputText("exact sent text"))
    with pytest.raises(RelationViolationError):
        owner.record_input_display(INPUT_ID, DisplayText("overwrite"), sent_text=SentInputText("different text"))
    with TranscriptRoutes(tmp_path).for_session("session") as routes:
        row = routes.input_display(INPUT_ID)
        assert row.text == "human display"
        assert row.sent_text_digest.matches("exact sent text")
        assert not row.sent_text_digest.matches("different text")
    with owner.snapshot() as connection:
        assert tuple(connection.execute(
            f'SELECT text, routing, sent_text_digest FROM "{InputDisplay.declared_name}"'
        ).fetchone()) == ("human display", None, TextDigest.of("exact sent text").value)


def test_empty_display_is_present_and_internal_display_remains_absent(tmp_path):
    owner = TranscriptRoutes(tmp_path)
    owner.record_input_display(INPUT_ID, DisplayText(""), sent_text=SentInputText(""))
    with owner.for_session("session") as routes:
        assert routes.input_display(INPUT_ID).text.present() == (DisplayText(""),)
        assert routes.input_display(INPUT_ID).sent_text_digest.matches("")
    internal = TranscriptRoutes(tmp_path / "internal")
    internal.record_input_display(INPUT_ID, NoDisplayText())
    with internal.for_session("session") as routes:
        assert routes.input_display(INPUT_ID).text.present() == ()


def test_reading_an_installed_store_writes_nothing(tmp_path, monkeypatch):
    import sqlite3

    from agent_comms.transcript_routes import TranscriptRoutes

    routing = TurnRouting(reply=MessageRoute("worker", ("#first",)))
    entry_id = FreshNativeId.fresh()
    TranscriptRoutes(tmp_path).record("session", (entry_id,), routing)
    TranscriptRoutes(tmp_path).record_input_display(INPUT_ID, DisplayText("shown"), sent_text=SentInputText("sent"))
    statements: list[str] = []
    connect = sqlite3.connect

    def traced(*arguments, **options):
        connection = connect(*arguments, **options)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    reader = TranscriptRoutes(tmp_path)
    with reader.for_session("session") as routes:
        assert entry_id.routing_in(routes) == routing
        assert routes.input_display(INPUT_ID).sent_text_digest.matches("sent")
    written = [sql for sql in statements
               if sql.lstrip().split(None, 1)[0].upper()
               in {"CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "REPLACE"}]
    assert statements and written == []
    assert any("query_only=ON" in sql for sql in statements)


def test_reading_a_differing_schema_refuses_without_repair(tmp_path):
    import sqlite3

    import pytest

    from agent_comms.transcript_routes import TranscriptRoutes

    TranscriptRoutes(tmp_path).record_input_display(INPUT_ID, DisplayText("shown"))
    path = TranscriptRoutes(tmp_path).path
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE transcript_route")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="differs from its declaration"):
        TranscriptRoutes(tmp_path).for_session("session")
    assert path.read_bytes() == before
