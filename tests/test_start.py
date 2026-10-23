
import asyncio
import sys

import pytest

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.tools import invoke_tool
from agent_comms.comms import wire
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.threads import Thread


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner fixture")
async def test_real_stopped_owner_starts_through_shared_agent_tool(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "pi")
    source = tmp_path / "saved.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    declared = RegistrySessionLocation.from_path(str(source), comms.root)
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path), session_file=declared))
    comms.registry.unregister("worker")
    try:
        result = await asyncio.to_thread(invoke_tool, comms, "comms_start", {"name": "worker"}, caller=NoThreadCaller())
        async with asyncio.timeout(10):
            while not socket_path(comms.root, result["pid"]).exists():
                assert comms.registry.require("worker").process_alive
                await asyncio.sleep(0.05)
        again = await asyncio.to_thread(invoke_tool, comms, "comms_start", {"name": "worker"}, caller=NoThreadCaller())
        assert not again["launched"] and again["pid"] == result["pid"]
        assert comms.registry.require("worker").session_file == declared.bound_to(comms.root)
    finally:
        await asyncio.to_thread(comms.owners.stop, "worker")
