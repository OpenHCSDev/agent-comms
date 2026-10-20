
import asyncio
import sys

import pytest

from agent_comms.agent_events import AgentInfo, ContextUsageObservation
from agent_comms.comms import wire
from agent_comms.pi_counts import PiCount
from agent_comms.threads import Thread
from agent_comms.native_arguments import GivenBinary


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner process fixture")
async def test_real_idle_owner_is_replaced_without_losing_session(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    session = tmp_path / "session.jsonl"
    session.write_text("")
    comms = wire(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path), session_file=str(session)))
    comms.agents.observe_runtime_info("worker", ContextUsageObservation(context_used=PiCount.from_wire(23)))
    comms.agents.observe_runtime_info(
        "worker", AgentInfo(model="test/model", context_size=PiCount.from_wire(100))
    )
    comms.ledger.merge({"saved": {"worker": ["work remains", 23]}}, "worker")
    metadata = comms.agents.runtime_info.path.read_bytes()
    collaboration = comms.ledger.path.read_bytes()
    owner = comms.owners.ensure_owner("worker", agent_bin="pi")
    process = owner.require_process()

    async def ready(pid):
        async with asyncio.timeout(10):
            while not socket_path(comms.root, pid).exists():
                assert comms.registry.require("worker").process_alive
                await asyncio.sleep(0.05)

    try:
        await ready(process.pid)
        receipts = await asyncio.to_thread(
            comms.owners.restart_owners, ["worker"], agent_bin=GivenBinary("pi")
        )
        assert receipts[0].previous_pid == process.pid
        assert receipts[0].pid != process.pid
        assert not owner.process_alive
        await ready(receipts[0].pid)
        assert comms.registry.require("worker").session_file == str(session)
        assert not comms.registry.require("worker").turn_record.read().executing
        assert comms.agents.runtime_info.path.read_bytes() == metadata
        assert comms.ledger.path.read_bytes() == collaboration
        reopened = wire(comms.root)
        assert reopened.agents.agent_info_of("worker").context_used == PiCount.from_wire(23)
        assert reopened.ledger.read().entries["saved"] == {"worker": ["work remains", 23]}
    finally:
        await asyncio.to_thread(comms.owners.stop, "worker")
