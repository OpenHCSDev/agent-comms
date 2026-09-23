import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_comms import Thread, ThreadRole, invoke_context_tool, invoke_tool, tool_catalog, wire
from agent_comms.declarations import ActiveTurn


def test_start_tool_reserves_one_owner_and_preserves_saved_state(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    saved = Thread(
        "worker",
        frozenset({"team"}),
        str(tmp_path),
        session_file="/saved.jsonl",
        model="test/model",
    )
    comms.register(saved)
    comms.registry.unregister("worker")
    launched = []

    def launch(thread, binary, arguments):
        launched.append(thread.name)
        assert binary == "test-pi"
        owner = replace(thread, pid=12345)
        comms.registry.register(owner)
        return owner

    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "test-pi")
    monkeypatch.setattr(comms, "_launch_owner_unlocked", launch)
    monkeypatch.setattr(comms, "_process_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(comms, "_is_local_participant", lambda thread: True)
    assert any(tool["name"] == "comms_start" for tool in tool_catalog())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: invoke_context_tool(comms, "comms_start", subject="worker"), range(2)
            )
        )
    assert launched == ["worker"]
    assert sorted(result["launched"] for result in results) == [False, True]
    assert {result["pid"] for result in results} == {12345}
    assert comms.registry.require("worker") == replace(saved, pid=12345)
    assert comms.registry.status("worker").active
    busy = replace(comms.registry.require("worker"), active_turn=ActiveTurn("busy", 12345))
    comms.registry.register(busy)
    assert invoke_tool(comms, "comms_start", {"name": "worker"})["launched"] is False
    assert comms.registry.require("worker") == busy


def test_start_refuses_archived_humans_and_unverifiable_live_pids(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread("worker", frozenset(), str(tmp_path), pid=os.getpid()))
    monkeypatch.setattr(comms, "_is_local_participant", lambda thread: False)
    with pytest.raises(ValueError, match="unverifiable"):
        comms.start("worker")
    comms.registry.unregister("worker")
    comms.archive("worker")
    with pytest.raises(ValueError, match="visible agent"):
        comms.start("worker")
    comms.registry.register(Thread("human", frozenset(), str(tmp_path), role=ThreadRole.USER))
    with pytest.raises(ValueError, match="visible agent"):
        comms.start("human")


def test_failed_launch_keeps_thread_stopped(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    comms.registry.unregister("worker")

    def fail(*args):
        raise OSError("Cannot launch owner")

    monkeypatch.setattr(comms, "_launch_owner_unlocked", fail)
    with pytest.raises(OSError, match="Cannot launch"):
        comms.start("worker")
    assert comms.registry.status("worker").value == "stopped"


def test_attachment_does_not_implicitly_start_a_stopped_or_archived_thread(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file="/saved.jsonl"))
    comms.registry.unregister("worker")
    monkeypatch.setattr(
        comms,
        "_launch_owner_unlocked",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unwanted launch")),
    )
    with pytest.raises(ValueError, match="explicit comms_start"):
        comms.ensure_owner("worker")
    comms.registry.archive("worker")
    with pytest.raises(ValueError, match="explicit comms_start"):
        comms.ensure_owner("worker")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner fixture")
async def test_real_stopped_owner_starts_through_shared_agent_tool(tmp_path, monkeypatch):
    from agent_comms.runtime import socket_path

    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/bin/echo")
    source = tmp_path / "saved.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(source)))
    comms.registry.unregister("worker")
    try:
        result = await asyncio.to_thread(invoke_tool, comms, "comms_start", {"name": "worker"})
        async with asyncio.timeout(10):
            while not socket_path(comms.root, result["pid"]).exists():
                assert comms._process_alive(result["pid"])
                await asyncio.sleep(0.05)
        again = await asyncio.to_thread(invoke_tool, comms, "comms_start", {"name": "worker"})
        assert not again["launched"] and again["pid"] == result["pid"]
        assert comms.registry.require("worker").session_file == str(source)
    finally:
        await asyncio.to_thread(comms.stop, "worker")
