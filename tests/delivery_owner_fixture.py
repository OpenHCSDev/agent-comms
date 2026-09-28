"""Real owner with a message queued behind its existing turn lock."""

import asyncio
import os
from contextlib import asynccontextmanager

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread


@asynccontextmanager
async def queued_delivery_owner(root):
    """A real incoming message waits behind the existing turn lock; no provider starts."""
    project = root / "project"
    project.mkdir(parents=True)
    comms = wire(root / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    session = (await owner.new_session(str(project))).session_id
    comms.threads.register(Thread("peer", frozenset(), str(project)))
    ledger = owner.inputs.dispositions
    admission = comms.registry.snapshot().admission_generations[session]
    for key in ("acp:earlier-unbound", "acp:earlier-bound"):
        ledger.record(key, seq=None, owner=session, admission=admission, target=session, text=key)
    ledger.bind(
        "acp:earlier-bound",
        admission=admission,
        turn_id="old-turn",
        native_id="a" * 32,
        text="old bound input",
    )
    lock = owner.turns.turn_locks.setdefault(session, asyncio.Lock())
    await lock.acquire()
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    try:
        incoming = comms.messaging.send_message("peer", session, "New input must remain awaiting")
        await owner.inputs.drain_inbox(session)
        assert owner.inputs.pending_turns[session][0].origin == incoming
        assert owner.inputs.wake_tasks[session] and not owner.inputs.wake_tasks[session].done()
        assert not owner.inputs.backend_inboxes
        yield owner, proxy, session, incoming
    finally:
        await proxy.close()
        await owner.shutdown()
        lock.release()
