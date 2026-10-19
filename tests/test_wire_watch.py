"""An idle owner wakes for durable wire changes without a 20 Hz scan."""

from unittest.mock import AsyncMock

import asyncio
import os
import sqlite3
from types import SimpleNamespace

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.threads import Thread
from agent_comms.wire_watch import WatchSource, WireChangeWatch, open_wire_watcher


def test_tick_revision_paths_watch_final_bus_publication(tmp_path, monkeypatch):
    config = tmp_path / "native-config"
    config.mkdir()
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    agent = CommsAgent(wire(tmp_path), runtime_enabled=False)
    paths = agent.inputs._tick_paths()
    declared = {
        *paths.registry,
        *paths.coordination,
        *paths.bus[1:],
        *paths.other,
    }
    watched = {
        source.directory / os.fsdecode(name)
        for source in paths.watch_sources
        for name in source.names
    }
    assert watched == declared
    modified = {
        source.directory / os.fsdecode(name)
        for source in paths.watch_sources
        if source.modified
        for name in source.names
    }
    assert modified == {*paths.coordination, *paths.goal_attempts}
    assert paths.bus[0] not in watched and paths.checkpoint not in watched


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unlink", "move-away"])
async def test_quiet_native_watch_wakes_when_auth_file_disappears(tmp_path, action):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    watcher = open_wire_watcher(WatchSource.file(auth))
    if not isinstance(watcher, WireChangeWatch):
        watcher.close()
        pytest.skip("Native file notifications are unavailable")
    waiting = None
    try:
        await watcher.prepare()
        waiting = asyncio.create_task(watcher.wait())
        # Neither a clock nor an unrelated file removal may wake this observer.
        unrelated = tmp_path / "unrelated"
        unrelated.write_text("unrelated")
        unrelated.unlink()
        await asyncio.sleep(0.1)
        assert not waiting.done()
        if action == "unlink":
            auth.unlink()
        else:
            auth.rename(tmp_path / "removed.json")
        assert await asyncio.wait_for(waiting, 1) is watcher
        assert not auth.exists()
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        watcher.close()


@pytest.mark.asyncio
async def test_relevant_write_burst_cannot_postpone_owner_wake(tmp_path):
    source = tmp_path / "shared-state"
    source.write_text("0")
    watcher = open_wire_watcher(WatchSource.modified_file(source))
    if not isinstance(watcher, WireChangeWatch):
        watcher.close()
        pytest.skip("Native file notifications are unavailable")

    async def keep_writing():
        deadline = asyncio.get_running_loop().time() + 0.25
        revision = 0
        while asyncio.get_running_loop().time() < deadline:
            source.write_text(str(revision))
            revision += 1
            await asyncio.sleep(0.005)

    writes = None
    try:
        await watcher.prepare()
        writes = asyncio.create_task(keep_writing())
        assert await asyncio.wait_for(watcher.changed.wait(), 0.15) is True
        assert not writes.done(), "the wake waited for unrelated writes to stop"
    finally:
        if writes is not None:
            writes.cancel()
            await asyncio.gather(writes, return_exceptions=True)
        watcher.close()


@pytest.mark.asyncio
async def test_private_guard_read_does_not_wake_its_own_wire_watcher(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    root_id = comms.messaging.initialize_private_initial_protocol()
    guard = PrivateRegistryGuard(comms.registry.store.path, root_id)
    watcher = open_wire_watcher(WatchSource.wire(tmp_path))
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
async def test_idle_owner_wakes_on_declared_sources_without_polling(tmp_path, monkeypatch):
    config = tmp_path / "native-config"
    config.mkdir()
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    probe = open_wire_watcher(WatchSource.wire(tmp_path))
    if not isinstance(probe, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    comms = wire(tmp_path)
    comms.registry.declare(
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.messaging.initialize_private_initial_protocol()
    agent = CommsAgent(comms, runtime_enabled=False)
    agent.sessions.bindings["owner"] = "owner"
    calls = 0

    async def drain(_session_id):
        nonlocal calls
        calls += 1
        return 0

    async def require_calls(expected):
        async with asyncio.timeout(1):
            while calls < expected:
                await asyncio.sleep(0.01)

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent.inputs, "drain_inbox", drain)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", noop)
    monkeypatch.setattr(agent.turns.goals, "schedule_goal", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent.inputs.effects.cursors, "delivery",
        lambda _session_id: SimpleNamespace(needs_refresh=False),
    )
    agent.inputs.ensure_live_drain("owner")
    task = agent.inputs.state("owner").drain_task
    try:
        await asyncio.sleep(1.2)
        assert calls == 1  # Healthy native notification has no speculative tick.
        (config / "auth.json").write_text("{}")
        await require_calls(2)
        with (tmp_path / "thread_read_markers.json").open("wb") as output:
            output.write(b"{}")
        with (tmp_path / ".registry.json.lock").open("ab"):
            pass
        await asyncio.sleep(0.1)
        assert calls == 2  # Viewer and lock files are not tick dependencies.
        with (tmp_path / "coordination.sqlite3").open("ab"):
            pass
        await require_calls(3)
        with (tmp_path / "private_bus_checkpoint.sqlite3").open("ab"):
            pass
        with (tmp_path / "bus.jsonl").open("ab"):
            pass
        await asyncio.sleep(0.1)
        assert calls == 3  # Intermediate bus files publish through the marker.
        comms.messaging.send("sender", "owner", "wake on final marker").message
        await require_calls(4)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recoverable_drain_failure_waits_for_dependency_change(
    tmp_path, monkeypatch,
):
    config = tmp_path / "native-config"
    config.mkdir()
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    agent = CommsAgent(comms, runtime_enabled=False)
    agent.sessions.bindings["owner"] = "owner"
    attempts = 0
    retried = asyncio.Event()

    async def drain(_session_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("controlled transient lock")
        retried.set()
        return 0

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent.inputs, "drain_inbox", drain)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", noop)
    monkeypatch.setattr(agent.turns.goals, "schedule_goal", AsyncMock(return_value=None))
    agent.inputs.ensure_live_drain("owner")
    task = agent.inputs.state("owner").drain_task
    try:
        await asyncio.sleep(0.15)
        assert attempts == 1
        (config / "auth.json").write_text("{}")
        await asyncio.wait_for(retried.wait(), 1)
        assert attempts == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_cursor_publish_retries_without_a_wake_task(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    agent = CommsAgent(comms, runtime_enabled=False)
    agent.sessions.bindings["owner"] = "owner"
    attempts = 0
    retried = asyncio.Event()

    async def drain(_session_id):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            retried.set()
        return 0

    async def noop(_session_id=None):
        return None

    monkeypatch.setattr(agent.inputs, "drain_inbox", drain)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", noop)
    monkeypatch.setattr(agent.turns.goals, "schedule_goal", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent.inputs.effects.cursors, "delivery",
        lambda _session_id: SimpleNamespace(needs_refresh=attempts < 2),
    )
    agent.inputs.ensure_live_drain("owner")
    task = agent.inputs.state("owner").drain_task
    try:
        await asyncio.wait_for(retried.wait(), 1)
        assert not any(state.wake_task for state in agent.inputs.drained.values())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_wake_completion_notifies_its_observer_once(tmp_path, monkeypatch):
    agent = CommsAgent(wire(tmp_path), runtime_enabled=False)
    notifications = []
    monkeypatch.setattr(agent.inputs, "wake_observer", notifications.append)
    task = asyncio.create_task(asyncio.sleep(0))
    agent.inputs.register_wake("owner", task)
    await task
    await asyncio.sleep(0)
    assert notifications == ["owner"]


@pytest.mark.asyncio
async def test_sidebar_time_expiry_does_not_depend_on_a_file_wake(monkeypatch):
    from agent_comms.ui_model import observation

    service = object.__new__(observation.ObservationService)
    service.closed = False
    service.observe_due = False
    service.changed = asyncio.Event()
    monkeypatch.setattr(observation.WireRevision, "expiry_seconds", 0.02)
    expiring = asyncio.create_task(service.expire())
    try:
        await asyncio.wait_for(service.changed.wait(), 0.2)
        assert service.observe_due
    finally:
        service.closed = True
        expiring.cancel()
        await asyncio.gather(expiring, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_idle_file_wait(tmp_path, monkeypatch):
    probe = open_wire_watcher(WatchSource.wire(tmp_path))
    if not isinstance(probe, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    probe.close()

    comms = wire(tmp_path)
    comms.registry.declare(
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
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


async def test_native_watch_reopens_when_a_source_directory_is_recreated(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    watch = open_wire_watcher(WatchSource.wire(root))
    if not isinstance(watch, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    reopened = restored = None
    try:
        await watch.prepare()
        root.rename(tmp_path / "moved")
        await asyncio.wait_for(watch.changed.wait(), 1)
        assert watch.invalid
        reopened = await watch.wait()
        assert isinstance(reopened, WireChangeWatch)
        assert watch.fd == -1

        await reopened.prepare()
        root.mkdir()
        await asyncio.wait_for(reopened.changed.wait(), 1)
        assert reopened.invalid
        restored = await reopened.wait()
        assert isinstance(restored, WireChangeWatch)
    finally:
        watch.close()
        if reopened is not None:
            reopened.close()
        if restored is not None:
            restored.close()


async def test_observation_cancellation_releases_actual_descriptor(tmp_path, monkeypatch):
    from contextlib import aclosing
    import agent_comms.wire_watch as wire_watch

    opened = []
    original = wire_watch.open_wire_watcher

    def capture(*sources):
        actual = original(*sources)
        opened.append(actual)
        return actual

    monkeypatch.setattr(wire_watch, "open_wire_watcher", capture)
    observed = asyncio.Event()

    async def observe():
        async with aclosing(
            wire_watch.WireWatch.observations(wire_watch.WatchSource.wire(tmp_path))
        ) as changes:
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


@pytest.mark.asyncio
async def test_taking_a_store_lock_does_not_wake_directory_watchers(tmp_path):
    from agent_comms.store_files import ExclusiveLock, SharedLock, _store_lock

    store = tmp_path / "registry.json"
    with _store_lock(store):  # the lock file exists before watching
        pass
    # Watch the lock file itself: any close-write of it would be a wake.
    watcher = open_wire_watcher(
        WatchSource.file(tmp_path / ".registry.json.lock")
    )
    if not isinstance(watcher, WireChangeWatch):
        pytest.skip("Native file notifications are unavailable")
    try:
        for mode in (SharedLock(), ExclusiveLock()) * 5:
            with _store_lock(store, mode=mode):
                pass
        await asyncio.sleep(0.05)
        assert not watcher.changed.is_set(), "a store lock woke every directory watcher"
    finally:
        watcher.close()
