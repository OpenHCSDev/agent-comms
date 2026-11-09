"""A contended trusted load must not suppress its later real cursor recovery."""



import asyncio

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.acp_extension import CursorAdvancedUpdate, decode_updates
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.owner_protocol import OwnerLine, OwnerUpdate
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.runtime_server import SocketClient
from bus_history import read_history
from delivery_owner_fixture import qualified_owner_launch, start_headless_owner  # noqa: F401


@pytest.fixture
def coordinator_writer(private_comms):
    with Coordination(str(private_comms.root / "coordination.sqlite3")) as store:
        yield store.session


async def test_trusted_load_recovers_after_real_sql_contention(
        tmp_path, private_comms, coordinator_writer, qualified_owner_launch):
    # Draining an owned inbox publishes native cursors, which only a private root may do.
    comms = private_comms
    owner = comms.registry.declare(
        # No live PID: acquire_thread returns a live thread untouched, reserving no owner lease.
        NativeThread('reader', frozenset(), Checkout(tmp_path))
    )
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.participants.register(
            owner.incarnation.lookup, committed=True
        )
    agent = CommsAgent(
        comms,
        auto_wake=False,
    )
    # The live observer acts only for a session under its own lease (#3982); a PID-only owner's
    # observer stops at owned(). start_owner publishes AcpSessionId(thread.name).
    session = AcpSessionId(owner.name)
    await start_headless_owner(agent, owner.name, qualified_owner_launch)
    attached = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        attached.set_result(writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    server_writer = await attached
    agent._runtime.clients[session] = {SocketClient(server_writer)}

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

    async def cancel_drains():
        drains = [task for state in agent.inputs.drained.values()
                  for task in state.observer.producers()]
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)

    try:
        await agent.inputs.drain_inbox(session)
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
        with coordinator_writer.writing():
            await agent.cursors.publish(session, owner.name)
        await agent.cursors.publish(session, owner.name)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(receive(), 0.05)
        # A settled turn cannot lose its cursor update to the same lock. The
        # idle observer must revisit the durable row once contention clears.
        with coordinator_writer.writing():
            await agent.cursors.publish(session, owner.name, selected_status="unavailable")
        await agent.cursors.refresh(session, owner.name)
        before = await receive(before.revision)
        assert before.observation.status == "none"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(receive(), 0.05)
        with coordinator_writer.writing():
            loaded = next(
                update.envelope
                for update in (await agent.cursors.trusted_metadata(owner.name, session))
                if isinstance(update, CursorAdvancedUpdate)
            )
            assert loaded.observation.status == "unavailable"
            assert loaded.scope == before.scope
            await agent.cursors.publish(session, owner.name)
        # The writer committed nothing, so no change marker wakes anyone: the
        # deferred read's own retry must publish once the lock clears.
        recovered = await receive(loaded.revision)
        assert recovered.observation.status == "none"
        assert recovered.scope == loaded.scope
        assert recovered.revision > loaded.revision
        await cancel_drains()
        await agent.inputs.drain_inbox(session)
        settled_revision = agent.cursors.delivery(session).revision
        for _ in range(20):
            await agent.inputs.drain_inbox(session)
        assert agent.cursors.delivery(session).revision == settled_revision
        assert read_history(comms) == []  # No input submitted or replayed.
    finally:
        await cancel_drains()
        server_writer.close()
        writer.close()
        await asyncio.gather(server_writer.wait_closed(), writer.wait_closed())
        server.close()
        await server.wait_closed()
