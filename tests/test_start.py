import asyncio
import sys

import pytest

from agent_comms import invoke_tool
from agent_comms.comms import wire
from agent_comms.threads import Thread


def test_attachment_does_not_implicitly_start_a_stopped_or_archived_thread(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.threads.register(
        Thread("worker", frozenset(), str(tmp_path), session_file="/saved.jsonl")
    )
    comms.registry.unregister("worker")
    monkeypatch.setattr(
        comms.owners,
        "_launch_owner_unlocked",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unwanted launch")),
    )
    with pytest.raises(ValueError, match="Explicit start"):
        comms.owners.ensure_owner("worker")
    comms.registry.archive("worker")
    with pytest.raises(ValueError, match="Explicit start"):
        comms.owners.ensure_owner("worker")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner fixture")
async def test_real_stopped_owner_starts_through_shared_agent_tool(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/bin/echo")
    source = tmp_path / "saved.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(source)))
    comms.registry.unregister("worker")
    try:
        result = await asyncio.to_thread(invoke_tool, comms, "comms_start", {"name": "worker"})
        async with asyncio.timeout(10):
            while not socket_path(comms.root, result["pid"]).exists():
                assert comms.registry.require("worker").process_alive
                await asyncio.sleep(0.05)
        again = await asyncio.to_thread(invoke_tool, comms, "comms_start", {"name": "worker"})
        assert not again["launched"] and again["pid"] == result["pid"]
        assert comms.registry.require("worker").session_file == str(source)
    finally:
        await asyncio.to_thread(comms.owners.stop, "worker")
