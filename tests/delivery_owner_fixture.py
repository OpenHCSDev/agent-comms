"""Real owner with a message queued behind its existing turn lock."""

import asyncio
import os
from contextlib import asynccontextmanager

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.runtime import RuntimeProxy, socket_path


@asynccontextmanager
async def queued_delivery_owner(root):
    """A real incoming message waits behind the existing turn lock; no provider starts."""
    project = root / "project"
    project.mkdir(parents=True)
    comms = wire(root / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    session = (await owner.new_session(str(project))).session_id
    comms.register(Thread("peer", frozenset(), str(project)))
    ledger = owner._dispositions
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
    lock = owner._turn_locks.setdefault(session, asyncio.Lock())
    await lock.acquire()
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    try:
        incoming = comms.send_message("peer", session, "New input must remain awaiting")
        await owner._drain_inbox(session)
        assert owner._pending_turns[session][0].origin == incoming
        assert owner._wake_tasks[session] and not owner._wake_tasks[session].done()
        assert not owner._backend_inboxes
        yield owner, proxy, session, incoming
    finally:
        await proxy.close()
        await owner.shutdown()
        lock.release()
