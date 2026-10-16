import asyncio
import sys
import os
from pathlib import Path

import pytest

from agent_comms.agent_events import AgentInfo

from agent_comms.threads import Thread
from agent_comms.comms import Comms
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.active_route import resolve_comms_route
from agent_comms.launch_runtime import LaunchRuntime


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner process fixture")
async def test_real_idle_owner_is_replaced_without_losing_session(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    session = tmp_path / "session.jsonl"
    session.write_text("")
    comms = Comms(resolve_comms_route(tmp_path / "wire"), runtime=LaunchRuntime.current())
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(comms.root, root_id, Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]))
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    comms.agents.observe_runtime_info(
        "worker", AgentInfo(model="test/model", session_name="preserved", context_used=23, context_size=100)
    )
    comms.ledger.merge({"saved": {"worker": ["work remains", 23]}}, "worker")
    metadata = comms.agents.runtime_info.path.read_bytes()
    collaboration = comms.ledger.path.read_bytes()
    owner = comms.owners.ensure_owner("worker", native=comms.owners.native_defaults)

    async def ready(pid):
        async with asyncio.timeout(10):
            while not socket_path(comms.root, pid).exists():
                assert comms.registry.require("worker").process_alive
                await asyncio.sleep(0.05)

    try:
        await ready(owner.pid)
        receipts = await asyncio.to_thread(
            comms.owners.restart_owners, ["worker"], agent_bin="pi"
        )
        assert receipts[0].previous_pid == owner.pid
        assert receipts[0].pid != owner.pid
        assert not owner.process_alive
        await ready(receipts[0].pid)
        assert comms.registry.require("worker").session_file == str(session)
        assert comms.registry.require("worker").active_turn is None
        assert comms.agents.runtime_info.path.read_bytes() == metadata
        assert comms.ledger.path.read_bytes() == collaboration
        reopened = Comms(resolve_comms_route(comms.root), runtime=LaunchRuntime.current())
        assert reopened.agents.agent_info_of("worker").context_used == 23
        assert reopened.ledger.read()["saved"] == {"worker": ["work remains", 23]}
    finally:
        await asyncio.to_thread(comms.owners.stop, "worker")
