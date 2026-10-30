
from agent_comms.repo import Checkout
import asyncio
import sys
from dataclasses import replace

import pytest

from agent_comms.repo import Repo
from agent_comms.storage_rules import StorageRule, StorageProjection
from agent_comms.shell_effects import AllocateStorageEffect, StorageDenied

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.tools import invoke_tool
from agent_comms.comms import wire
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.threads import Thread


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner fixture")
async def test_real_stopped_owner_with_large_checkout_starts_through_shared_agent_tool(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "pi")
    Repo.git(tmp_path, "init", "-q")
    with (tmp_path / "existing-project-data").open("wb") as payload:
        payload.truncate(4 * 2**30)  # Sparse: exercises the old cap without allocating 4 GiB.
    source = tmp_path / "saved.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    comms.registry.storage_rule = replace(
        comms.registry.storage_rule, volume=tmp_path, floor_gib=0, memory_gib=0)
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


def test_start_and_allocating_tools_keep_the_real_free_space_floor(tmp_path, monkeypatch):
    rule = StorageRule(volume=tmp_path)
    assert rule.floor_gib == 10
    monkeypatch.setattr(StorageRule, "free_gib", lambda self: 9.9)
    comms = wire(tmp_path / "wire")
    thread = comms.registry.declare(Thread("worker", frozenset(), Checkout(tmp_path)))
    with pytest.raises(StorageDenied, match="10 GiB free"):
        with rule.room_for_start(thread, comms.root):
            pytest.fail("A stopped owner must not start below the volume floor")
    with pytest.raises(StorageDenied, match="10 GiB free"):
        StorageProjection(rule, thread, comms).check((AllocateStorageEffect(),))
