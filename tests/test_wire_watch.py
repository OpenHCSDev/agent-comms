"""An idle owner wakes for durable wire changes without a 20 Hz scan."""

from unittest.mock import AsyncMock

import asyncio
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.threads import Thread
from agent_comms.wire_watch import WireChangeWatch, open_wire_watcher


@pytest.mark.asyncio
async def test_private_guard_read_does_not_wake_its_own_wire_watcher(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    root_id = comms.messaging.initialize_private_initial_protocol()
    guard = PrivateRegistryGuard(comms.registry.store.path, root_id)
    watcher = open_wire_watcher(tmp_path)
    if not isinstance(watcher, WireChangeWatch):
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
    if not isinstance(probe, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    comms = wire(tmp_path)
    comms.registry.declare(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    agent = CommsAgent(comms, runtime_enabled=False)
    agent.sessions.bindings["owner"] = "owner"
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
    monkeypatch.setattr(agent.turns.goals, "schedule_goal", AsyncMock(return_value=None))
    agent.inputs.ensure_live_drain("owner")
    task = agent.inputs.drain_tasks["owner"]
    try:
        await asyncio.sleep(0.2)
        assert calls == 1
        with (tmp_path / "thread_read_markers.json").open("wb") as output:
            output.write(b"{}")
        for name in (
            "coordination.sqlite3",
            ".registry.json.lock",
            "private_bus_checkpoint.sqlite3",
        ):
            with (tmp_path / name).open("ab"):
                pass
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
    if not isinstance(probe, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    comms = wire(tmp_path)
    comms.registry.declare(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    agent = CommsAgent(comms, runtime_enabled=False)
    agent.sessions.bindings["owner"] = "owner"
    entered = asyncio.Event()

    async def drain(_session_id):
        entered.set()
        return 0

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent.inputs, "drain_inbox", drain)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", noop)
    monkeypatch.setattr(agent.turns.goals, "schedule_goal", AsyncMock(return_value=None))
    agent.inputs.ensure_live_drain("owner")
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.sleep(0)
    await asyncio.wait_for(agent.shutdown(), timeout=1)


async def test_native_watch_invalidation_and_polling_own_resource_cleanup(tmp_path):
    from agent_comms.wire_watch import PollingWireWatch

    root = tmp_path / "wire"
    root.mkdir()
    watch = open_wire_watcher(root)
    if not isinstance(watch, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    descriptor = watch.fd
    try:
        await watch.prepare()
        root.rename(tmp_path / "moved")
        await asyncio.wait_for(watch.changed.wait(), 1)
        assert watch.invalid
        polling = await watch.wait()
        assert isinstance(polling, PollingWireWatch)
        with pytest.raises(OSError):
            os.fstat(descriptor)
        # The actual unavailable-root factory has the same concrete behavior.
        absent = open_wire_watcher(root)
        assert isinstance(absent, PollingWireWatch)
        for source in (polling, absent):
            await asyncio.wait_for(source.prepare(), 1)
            assert await source.wait() is source
            source.close()
    finally:
        watch.close()


async def test_observation_cancellation_releases_actual_descriptor(tmp_path, monkeypatch):
    from contextlib import aclosing
    from agent_comms import wire_watch

    opened = []
    original = wire_watch.open_wire_watcher

    def capture(root):
        actual = original(root)
        opened.append(actual)
        return actual

    monkeypatch.setattr(wire_watch, "open_wire_watcher", capture)
    observed = asyncio.Event()

    async def observe():
        async with aclosing(wire_watch.WireWatch.observations(tmp_path)) as changes:
            async for _ in changes:
                observed.set()

    task = asyncio.create_task(observe())
    try:
        await asyncio.wait_for(observed.wait(), 1)
        if not isinstance(opened[0], WireChangeWatch):
            pytest.skip("Native file notifications are unavailable")
        descriptor = opened[0].fd
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
