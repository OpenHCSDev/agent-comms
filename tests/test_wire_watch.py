"""An idle owner wakes for durable wire changes without a 20 Hz scan."""

import asyncio
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.declarations import Thread
from agent_comms.operations import Comms, wire
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.wire_watch import open_wire_watcher


@pytest.mark.asyncio
async def test_private_guard_read_does_not_wake_its_own_wire_watcher(tmp_path):
    comms = Comms(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    root_id = comms.initialize_private_initial_protocol()
    guard = PrivateRegistryGuard(comms.registry.store.path, root_id)
    watcher = open_wire_watcher(tmp_path)
    if watcher is None:
        pytest.skip("Native file notifications are unavailable")
    try:
        for _ in range(10):
            guard.verify()
        await asyncio.sleep(0.05)
        assert not watcher.changed.is_set()

        # The watch is active: a writable close of the same guard is work.
        descriptor = os.open(guard.path, os.O_WRONLY)
        os.close(descriptor)
        await asyncio.wait_for(watcher.changed.wait(), timeout=1)
    finally:
        watcher.close()


@pytest.mark.asyncio
async def test_idle_owner_wakes_on_bus_append_without_polling(tmp_path, monkeypatch):
    probe = open_wire_watcher(tmp_path)
    if probe is None:
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    agent = CommsAgent(wire(tmp_path), runtime_enabled=False)
    calls = 0
    woke = asyncio.Event()

    async def drain(_session_id):
        nonlocal calls
        calls += 1
        if calls >= 2:
            woke.set()
        return 0

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent.inputs, "drain_inbox", drain)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", noop)
    monkeypatch.setattr(agent.turns, "schedule_goal", lambda _session_id: None)
    monkeypatch.setattr(agent.sessions.config, "refresh_auth_models", noop)
    agent.inputs.ensure_live_drain("owner")
    task = agent.inputs.drain_tasks["owner"]
    try:
        await asyncio.sleep(0.2)
        assert calls == 1
        with (tmp_path / "thread_read_markers.json").open("wb") as output:
            output.write(b"{}")
        await asyncio.sleep(0.1)
        assert calls == 1  # A viewer cursor is not new work for the owner.
        with (tmp_path / "bus.jsonl").open("ab") as output:
            output.write(b"{}\n")
        await asyncio.wait_for(woke.wait(), timeout=0.5)
        assert calls == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_idle_file_wait(tmp_path, monkeypatch):
    probe = open_wire_watcher(tmp_path)
    if probe is None:
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    agent = CommsAgent(wire(tmp_path), runtime_enabled=False)
    entered = asyncio.Event()

    async def drain(_session_id):
        entered.set()
        return 0

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent.inputs, "drain_inbox", drain)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", noop)
    monkeypatch.setattr(agent.turns, "schedule_goal", lambda _session_id: None)
    monkeypatch.setattr(agent.sessions.config, "refresh_auth_models", noop)
    agent.inputs.ensure_live_drain("owner")
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.sleep(0)
    await asyncio.wait_for(agent.shutdown(), timeout=1)
