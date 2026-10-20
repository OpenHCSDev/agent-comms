"""Canonical saved clocks survive projection, ACP transport and page rebuild."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime

import pytest

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.field_codec import MalformedRecord, RecordedTimestamp
from agent_comms.native_entries import NativeEntry, TranscriptProjection
from agent_comms.routing import TurnRouting
from agent_comms.threads import Thread
from agent_comms.transcript_events import AssistantTranscript



@pytest.mark.parametrize("stamp", ["2026-09-28T23:12:33.123Z", "2026-09-28T19:12:33.123-04:00"])
def test_original_record_clock_survives_split_and_routed_projection(tmp_path, stamp):
    comms = wire(tmp_path / "wire")
    for name in ("peer", "owner"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    requests = tuple(
        comms.messaging.send("peer", "owner", text).message for text in ("first", "second")
    )
    entry = NativeEntry.from_wire(
        {
            "type": "message",
            "timestamp": stamp,
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "native prompt"},
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                ],
            },
        }
    )
    expected = datetime.fromisoformat(stamp).timestamp()
    for context in (TranscriptProjection(position=0), TranscriptProjection(position=0, routing=TurnRouting(requests))):
        events = entry.events(context)
        assert len(events) == 2
        assert all(event.timestamp == expected for event in events)
        assert entry.timestamp == RecordedTimestamp(stamp)  # Native external field stays exact.
    compacted = NativeEntry.from_wire(
        {"type": "compaction", "timestamp": stamp, "summary": "kept context"}
    )
    assert compacted.events(TranscriptProjection(position=0))[0].timestamp == expected

    first = AssistantTranscript("start", timestamp=expected, position=0)
    assert first.merge(replace(first, text="continued")).timestamp == expected
    assert first.merge(replace(first, timestamp=expected + 1)) is None


def test_new_native_entry_inherits_original_clock_projection():
    class ExternalNoteEntry(NativeEntry):
        def _events(self, context, timestamp):
            return [AssistantTranscript("a new external note", position=context.position, timestamp=timestamp)]

    # The inherited entry owner decodes the source clock before constructing any content.
    stamp = "2026-09-28T23:12:33.123Z"
    expected = datetime.fromisoformat(stamp).timestamp()
    note = ExternalNoteEntry(timestamp=RecordedTimestamp(stamp))
    assert note.events(TranscriptProjection(position=0))[0].timestamp == expected


@pytest.mark.parametrize("stamp", [None, "not-a-time", "2026-09-28T23:12:33"])
def test_an_entry_without_an_absolute_recorded_clock_is_refused(stamp):
    with pytest.raises(MalformedRecord):
        NativeEntry.from_wire(
            {"type": "message", "timestamp": stamp, "message": {"role": "user", "content": "preserved"}}
        )


async def test_actual_native_saved_clocks_survive_acp_snapshot_and_rebuild(
    native_backend, monkeypatch
):
    native = native_backend
    assert (await native.run("Saved clock must survive returning to history"))[-1].ok
    original = native.session.read_bytes()
    proof = native.session.with_suffix(native.session.suffix + ".input-proof")
    original_proof = proof.read_bytes()
    records = [json.loads(line) for line in original.splitlines()]
    expected = [
        datetime.fromisoformat(row["timestamp"]).timestamp()
        for row in records
        if row["type"] == "message"
    ]
    assert len(expected) == 2
    comms = wire(native.root)
    comms.registry.declare(
        comms.registry.new_thread('clock', frozenset(), str(native.project), session_file=str(native.session))
    )
    page = comms.transcripts.thread_transcript_page("clock")
    assert [event.timestamp for event in page.events] == expected
    # Real official ACP byte transport, production load/replay path, no mocked
    # replay callback or fabricated snapshot. Native/model work stays local.
    import acp

    from delivery_owner_fixture import canonical_agent

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    owner = canonical_agent(comms, auto_wake=False)
    snapshots = asyncio.Queue()

    class Client(acp.Client):
        async def session_update(self, session_id, update, **kwargs):
            for fact in decode_updates(update.field_meta):
                if isinstance(fact, TranscriptSnapshotUpdate):
                    snapshots.put_nowait(fact.page)

    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await acp.run_agent(owner, writer, reader)
        finally:
            writer.close()
            await writer.wait_closed()
            connections.remove(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    client = acp.connect_to_agent(Client(), writer, reader)
    try:
        async with asyncio.timeout(15):
            await client.initialize(protocol_version=1)
            await client.load_session(cwd=str(native.project), session_id="clock", mcp_servers=[])
            received = await snapshots.get()
            assert received.events == page.events
            await client.load_session(cwd=str(native.project), session_id="clock", mcp_servers=[])
            assert (await snapshots.get()).events == received.events
        rebuilt = wire(native.root).transcripts.thread_transcript_page("clock", through=page.after)
        assert rebuilt.events == received.events
    finally:
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(3):
            await client.close()
        server.close()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*tuple(connections), return_exceptions=True)
        await server.wait_closed()
        await owner.shutdown()
    assert native.session.read_bytes() == original
    assert proof.read_bytes() == original_proof
    assert native.provider.posts == 1
    print(
        f"actual native clocks={expected}; ACP snapshot + rebuild exact; native history/proof unchanged"
    )


@pytest.mark.parametrize('source', ('claude', 'codex'))
@pytest.mark.parametrize('stamp', (None, 'missing'))
def test_external_source_without_a_time_refuses_before_event_construction(source, stamp):
    from pydantic import ValidationError
    from agent_comms.cli_bridges.claude_protocol import TimedRecord
    from agent_comms.cli_bridges.codex_rollout import ROLLOUT_RECORD
    from agent_comms.recorded_time import UnrecordedTimeError

    record = ({'uuid': 'record', 'position': 0} if source == 'claude' else
              {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': []}})
    if stamp is None:
        record['timestamp'] = None
    decode = TimedRecord.model_validate if source == 'claude' else ROLLOUT_RECORD.validate_python
    with pytest.raises(ValidationError) as refused:
        decode(record)
    assert any(isinstance(error.get('ctx', {}).get('error'), UnrecordedTimeError)
               for error in refused.value.errors())


def test_codex_rollout_projects_the_enclosing_record_clock():
    from agent_comms.cli_bridges.codex_rollout import ROLLOUT_RECORD, RolloutState

    record = ROLLOUT_RECORD.validate_python({
        'type': 'response_item', 'timestamp': '2026-10-19T18:00:00Z',
        'payload': {'type': 'message', 'role': 'assistant', 'id': 'item',
                    'content': [{'text': 'saved source text'}]},
    })
    update, = record.updates(RolloutState(), 17)
    assert update.events[0].timestamp == datetime.fromisoformat('2026-10-19T18:00:00Z').timestamp()


def test_malformed_shell_words_remain_visible_to_the_install_rule():
    from agent_comms.commander.send_guard import InstallCommand, simple_commands

    command = 'uv pip install package "unterminated'
    words, = simple_commands(command)
    assert words == tuple(command.split())
    assert InstallCommand.runs(words)
