import asyncio
import sys

import pytest

from agent_comms.comms import wire
from agent_comms.threads import Thread


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner process fixture")
async def test_real_idle_owner_is_replaced_without_losing_session(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    session = tmp_path / "session.jsonl"
    session.write_text("")
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    comms.agents.set_agent_info(
        "worker", model="test/model", session_name="preserved", context_used=23, context_size=100
    )
    comms.ledger.merge({"saved": {"worker": ["work remains", 23]}}, "worker")
    metadata = comms.agents.runtime_info.path.read_bytes()
    collaboration = comms.ledger.path.read_bytes()
    owner = comms.owners.ensure_owner("worker", agent_bin="/bin/echo")

    async def ready(pid):
        async with asyncio.timeout(10):
            while not socket_path(comms.root, pid).exists():
                assert comms.registry.require("worker").process_alive
                await asyncio.sleep(0.05)

    try:
        await ready(owner.pid)
        receipts = await asyncio.to_thread(
            comms.owners.restart_owners, ["worker"], agent_bin="/bin/echo"
        )
        assert receipts[0].previous_pid == owner.pid
        assert receipts[0].pid != owner.pid
        assert not owner.process_alive
        await ready(receipts[0].pid)
        assert comms.registry.require("worker").session_file == str(session)
        assert comms.registry.require("worker").active_turn is None
        assert comms.agents.runtime_info.path.read_bytes() == metadata
        assert comms.ledger.path.read_bytes() == collaboration
        reopened = wire(comms.root)
        assert reopened.agents.agent_info_of("worker").context_used == 23
        assert reopened.ledger.read()["saved"] == {"worker": ["work remains", 23]}
    finally:
        await asyncio.to_thread(comms.owners.stop, "worker")
