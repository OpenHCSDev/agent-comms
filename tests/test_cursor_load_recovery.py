"""A contended trusted load must not suppress its later real cursor recovery."""

from agent_comms.bus import Bus

from bus_history import read_history
import asyncio
import os

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import CursorAdvancedUpdate, decode_updates
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.owner_protocol import OwnerLine, OwnerUpdate
from agent_comms.runtime import SocketClient
from agent_comms.store_files import _store_lock


async def test_trusted_load_recovers_after_real_flock_contention(tmp_path):
    comms = Comms(tmp_path / "wire")
    owner = comms.registry.new_thread('reader', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(owner)
    Bus.install(Bus.path_in(comms.root))
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.participants.register(
            RecordedThreadIncarnation("fixture", owner.created_at).lookup, committed=True
        )
    agent = CommsAgent(
        comms,
        auto_wake=False,
    )
    agent.sessions.bindings[owner.name] = owner.name
    attached = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        attached.set_result(writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    server_writer = await attached
    agent._runtime.clients[owner.name] = {SocketClient(server_writer)}

    async def receive(after=0):
        async with asyncio.timeout(2.5):
            while True:
                line = OwnerLine.read(await reader.readline())
                assert isinstance(line, OwnerUpdate)
                for update in decode_updates(line.meta):
                    if (
                        isinstance(update, CursorAdvancedUpdate)
                        and update.envelope.revision > after
                    ):
                        return update.envelope

    try:
        await agent.inputs.drain_inbox(owner.name)
        before = await receive()
        assert before.observation.status == "none"
        # Drain initial queue/cursor publications before observing idle effects.
        while True:
            try:
                before = await asyncio.wait_for(receive(), 0.05)
            except TimeoutError:
                break
        # A periodic observation deferred by a real writer did not invalidate
        # the last trusted owner snapshot. After release, no duplicate should
        # reach the socket and schedule idle reader preparation.
        with _store_lock(comms.bus.bus.path):
            await agent.cursors.publish(owner.name, owner.name)
        await agent.cursors.publish(owner.name, owner.name)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(receive(), 0.05)
        # A settled turn cannot lose its cursor update to the same lock. The
        # idle observer must revisit the durable row once contention clears.
        with _store_lock(comms.bus.bus.path):
            await agent.cursors.publish(owner.name, owner.name, selected_status="unavailable")
        await agent.cursors.refresh(owner.name, owner.name)
        before = await receive(before.revision)
        assert before.observation.status == "none"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(receive(), 0.05)
        with _store_lock(comms.bus.bus.path):
            loaded = next(
                update.envelope
                for update in (await agent.cursors.trusted_metadata(owner.name, owner.name))
                if isinstance(update, CursorAdvancedUpdate)
            )
            assert loaded.observation.status == "unavailable"
            assert loaded.scope == before.scope
            await agent.cursors.publish(owner.name, owner.name)
        agent.inputs.ensure_live_drain(owner.name)
        recovered = await receive(loaded.revision)
        assert recovered.observation.status == "none"
        assert recovered.scope == loaded.scope
        assert recovered.revision > loaded.revision
        drains = [state.drain_task for state in agent.inputs.drained.values() if state.drain_task]
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
        await agent.inputs.drain_inbox(owner.name)
        settled_revision = agent.cursors.delivery(owner.name).revision
        for _ in range(20):
            await agent.inputs.drain_inbox(owner.name)
        assert agent.cursors.delivery(owner.name).revision == settled_revision
        assert read_history(comms) == []  # No input submitted or replayed.
    finally:
        drains = [state.drain_task for state in agent.inputs.drained.values() if state.drain_task]
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
        server_writer.close()
        writer.close()
        await asyncio.gather(server_writer.wait_closed(), writer.wait_closed())
        server.close()
        await server.wait_closed()
