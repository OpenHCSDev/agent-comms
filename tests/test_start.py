import asyncio
import sys
import os
from pathlib import Path

import pytest

from agent_comms import invoke_tool

from agent_comms.threads import Thread
from agent_comms.comms import Comms
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.active_route import resolve_comms_route
from agent_comms.launch_runtime import LaunchRuntime


def test_attachment_does_not_implicitly_start_a_stopped_or_archived_thread(tmp_path, monkeypatch):
    comms = Comms(resolve_comms_route(tmp_path), runtime=LaunchRuntime.current())
    comms.registry.declare(
        Thread("worker", frozenset(), str(tmp_path), session_file="/saved.jsonl")
    )
    comms.registry.unregister("worker")
    monkeypatch.setattr(
        comms.owners,
        "_launch_owner_unlocked",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unwanted launch")),
    )
    with pytest.raises(ValueError, match="Explicit start"):
        comms.owners.ensure_owner("worker", native=comms.owners.native_defaults)
    comms.registry.archive("worker")
    with pytest.raises(ValueError, match="Explicit start"):
        comms.owners.ensure_owner("worker", native=comms.owners.native_defaults)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner fixture")
async def test_real_stopped_owner_starts_through_shared_agent_tool(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    source = tmp_path / "saved.jsonl"
    source.touch()
    comms = Comms(resolve_comms_route(tmp_path / "wire"), runtime=LaunchRuntime.current())
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(
        comms.root, root_id, Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    )
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(source)))
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
