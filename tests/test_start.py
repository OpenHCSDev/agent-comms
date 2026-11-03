import asyncio
import sys
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.repo import Checkout, Repo
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.threads import Thread
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.tools import invoke_tool


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner fixture")
async def test_real_stopped_owner_with_large_checkout_starts_through_shared_agent_tool(tmp_path, monkeypatch, pinned_native):
    from agent_comms.owner_connection import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "pi")
    Repo.git(tmp_path, "init", "-q")
    with (tmp_path / "existing-project-data").open("wb") as payload:
        payload.truncate(4 * 2**30)  # Sparse: exercises the old cap without allocating 4 GiB.
    source = tmp_path / "saved.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    comms.registry.storage_rule = replace(
        comms.registry.storage_rule, volume=tmp_path, floor_gib=2**40, memory_gib=0)
    declared = RegistrySessionLocation.from_path(str(source), comms.root)
    comms.registry.declare(Thread('worker', frozenset(), Checkout(tmp_path), session_file=declared))
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
