"""A contended trusted load must not suppress its later real cursor recovery."""
import asyncio
import json
import os

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import CursorAdvancedUpdate, decode_updates
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_store import MutationStore
from agent_comms.runtime import SocketClient
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread


async def test_trusted_load_recovers_after_real_flock_contention(tmp_path):
    comms = Comms(tmp_path / 'wire')
    owner = Thread('reader', frozenset(), str(tmp_path),
                   process_identity=ProcessIdentity.capture(os.getpid()))
    comms.threads.register(owner)
    root_id = comms.messaging.initialize_private_initial_protocol()
    with MutationStore(str(comms.root / 'coordination.sqlite3')) as store:
        store.register_participant(stable_thread_lookup(owner.created_at),
                                   owner.name, owner.name, committed=True)
    agent = CommsAgent(comms, auto_wake=False, private_nk_wire_root_id=root_id,
                       private_nk_native_package=tmp_path / 'unused-native-package')
    agent.sessions.bindings[owner.name] = owner.name
    attached = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        attached.set_result(writer)

    server = await asyncio.start_server(accept, '127.0.0.1', 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    server_writer = await attached
    agent._runtime.clients[owner.name] = {SocketClient(server_writer)}

    async def receive():
        payload = json.loads(await asyncio.wait_for(reader.readline(), 1))
        return next(update.envelope for update in decode_updates(payload['update']['_meta'])
                    if isinstance(update, CursorAdvancedUpdate))

    try:
        await agent._publish_private_cursor(owner.name, owner.name)
        before = await receive()
        assert before.observation.status == 'none'
        with _store_lock(comms.root / 'bus.jsonl'):
            loaded = next(update.envelope for update in
                          agent._session_runtime_metadata(owner.name, owner.name)
                          if isinstance(update, CursorAdvancedUpdate))
            assert loaded.observation.status == 'unavailable'
            assert loaded.scope == before.scope
            await agent._publish_private_cursor(owner.name, owner.name)
        await agent._publish_private_cursor(owner.name, owner.name)
        recovered = await receive()
        assert recovered.observation.status == 'none'
        assert recovered.scope == loaded.scope
        assert recovered.revision > loaded.revision
        assert comms.views.full_history() == []  # No input submitted or replayed.
    finally:
        server_writer.close()
        writer.close()
        await asyncio.gather(server_writer.wait_closed(), writer.wait_closed())
        server.close()
        await server.wait_closed()
