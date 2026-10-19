"""Native drafts reuse the external store's declared schema and turn connection."""
import asyncio
from types import SimpleNamespace

from agent_comms.external_transcript import DraftRow, ExternalTranscript, TurnDraftPublication
from agent_comms.native_transcript import NativeTranscript
from agent_comms.pi_payloads import TextDelta
from agent_comms.tracked_turn import TrackedTurnSession
import agent_comms.tracked_turn as tracked


def test_native_draft_growth_throttle_and_retirement(tmp_path, monkeypatch):
    store = ExternalTranscript(tmp_path, 'a' * 32)
    source = NativeTranscript(tmp_path / 'session.jsonl')
    assert DraftRow.declared_name == 'external_draft'
    with store.writer_connection() as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        publication = TurnDraftPublication(store.lookup, connection, 'turn')
        publication.begin_message(1.0)
        turn = object.__new__(TrackedTurnSession)
        turn.text_parts = []
        turn.draft_publication = publication
        turn.draft_start = 0
        turn.draft_timestamp = 1.0
        turn.draft_written_at = 0.0
        clock = iter((10.0, 10.02, 10.11))
        monkeypatch.setattr(tracked, 'time', SimpleNamespace(monotonic=lambda: next(clock)))

        async def stream():
            await turn.text_delta(TextDelta(delta='one'))
            old = source.current_draft(tmp_path, store.lookup)
            await turn.text_delta(TextDelta(delta=' two'))
            assert source.current_draft(tmp_path, store.lookup).events[0].text == 'one'
            await turn.text_delta(TextDelta(delta=' three'))
            assert source.current_draft(tmp_path, store.lookup).events[0].text == 'one two three'
            publication.retire_message()
            assert source.current_draft(tmp_path, store.lookup).external_row() is None
            publication.begin_message(2.0)
            publication.append_text('new message')
            publication.publish()
            old.end_turn(store)
            assert source.current_draft(tmp_path, store.lookup).events[0].text == 'new message'
            publication.retire_message()

        asyncio.run(stream())
        assert publication.connection is connection
        assert not any('CREATE ' in statement.upper() for statement in statements)
    assert source.current_draft(tmp_path, store.lookup).external_row() is None
