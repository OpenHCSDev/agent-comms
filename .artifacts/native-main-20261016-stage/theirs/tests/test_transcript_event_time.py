"""Canonical saved clocks survive projection, ACP transport and page rebuild."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime

import pytest

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.native_entries import NativeEntry, TranscriptProjection
from agent_comms.routing import TurnRouting
from agent_comms.threads import Thread
from agent_comms.transcript_events import AssistantTranscript, UserTranscript

pytest_plugins = ("test_backend_native_lifecycle",)


@pytest.mark.parametrize("stamp", ["2026-09-28T23:12:33.123Z", "2026-09-28T19:12:33.123-04:00"])
def test_original_record_clock_survives_split_and_routed_projection(tmp_path, stamp):
    comms = wire(tmp_path / "wire")
    for name in ("peer", "owner"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    requests = tuple(
        comms.messaging.send_message("peer", "owner", text) for text in ("first", "second")
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
    for context in (TranscriptProjection(), TranscriptProjection(routing=TurnRouting(requests))):
        events = entry.events(context)
        assert len(events) == 2
        assert all(event.timestamp == expected for event in events)
        assert entry.timestamp == stamp  # Native external field stays exact.
    compacted = NativeEntry.from_wire(
        {"type": "compaction", "timestamp": stamp, "summary": "kept context"}
    )
    assert compacted.events(TranscriptProjection())[0].timestamp == expected

    first = AssistantTranscript("start", timestamp=expected)
    assert first.merge(replace(first, text="continued")).timestamp == expected
    assert first.merge(replace(first, timestamp=expected + 1)) is None


def test_new_native_entry_inherits_original_clock_projection():
    class ExternalNoteEntry(NativeEntry):
        def _events(self, context):
            return [AssistantTranscript("a new external note")]

    # Adding a content declaration does not add another clock propagation site.
    stamp = "2026-09-28T23:12:33.123Z"
    expected = datetime.fromisoformat(stamp).timestamp()
    note = ExternalNoteEntry(timestamp=stamp)
    assert note.events(TranscriptProjection())[0].timestamp == expected


@pytest.mark.parametrize("stamp", [None, "not-a-time", "2026-09-28T23:12:33"])
def test_unrecorded_clock_remains_unknown_without_losing_history(stamp):
    entry = NativeEntry.from_wire(
        {"type": "message", "timestamp": stamp, "message": {"role": "user", "content": "preserved"}}
    )
    assert entry.events(TranscriptProjection()) == [UserTranscript("preserved", timestamp=None)]


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
        Thread("clock", frozenset(), str(native.project), session_file=str(native.session))
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
            assert received == page
            await client.load_session(cwd=str(native.project), session_id="clock", mcp_servers=[])
            assert await snapshots.get() == received
        rebuilt = wire(native.root).transcripts.thread_transcript_page("clock", through=page.after)
        assert rebuilt == received
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
