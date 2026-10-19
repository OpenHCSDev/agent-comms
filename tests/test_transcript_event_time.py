"""Canonical saved clocks survive projection, ACP transport and page rebuild."""

import asyncio
import json
from datetime import datetime

import pytest

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.field_codec import MalformedRecord
from agent_comms.native_entries import NativeEntry


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
