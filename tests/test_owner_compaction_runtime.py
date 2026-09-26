"""Provider-free cancellation controls for the real owner worker lifetime."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent_comms import owner_compaction_runtime
from agent_comms.owner_compaction_prepare import NativePreparation
from agent_comms.owner_compaction_runtime import compact_owner_once


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["owner", "inner_wrapper", "all_tasks"])
async def test_owner_lock_joins_underlying_worker_not_cancelled_asyncio_wrapper(
    monkeypatch, shutdown
):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    turn_lock = asyncio.Lock()
    next_entered = asyncio.Event()
    wrappers = []
    wrap = asyncio.wrap_future

    def retained_wrapper(future):
        result = wrap(future)
        wrappers.append(result)
        return result

    monkeypatch.setattr(owner_compaction_runtime.asyncio, "wrap_future", retained_wrapper)

    class Bridge:
        def prepare_source(self, *_args, **_kwargs):
            return (
                NativePreparation("session-id", {"sessionFile": "/tmp/fake-saved"}, 1, False),
                object(),
            )

        def commit(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=4), "test release never arrived"
            finished.set()
            return SimpleNamespace(status="committed")

    class Persistent:
        async def discard_for_external_write(self, *_args):
            pass

    async def synthetic_summary(_metadata):
        return "synthetic, no provider"

    async def owner():
        async with turn_lock:
            await compact_owner_once(Bridge(), object(), 1, Persistent(), synthetic_summary)

    task = asyncio.create_task(owner(), name="actual-owner-turn")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert len(wrappers) == 1 and turn_lock.locked()
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        if shutdown == "inner_wrapper":
            # Stronger than all-tasks cancellation: even direct cancellation of
            # the asyncio Future must not mark its OS worker as complete.
            wrappers[0].cancel()
        elif shutdown == "all_tasks":
            extra = asyncio.create_task(asyncio.Event().wait(), name="unrelated-shutdown-task")
            for candidate in tuple(asyncio.all_tasks()):
                if candidate is not asyncio.current_task():
                    candidate.cancel()
            await asyncio.gather(extra, return_exceptions=True)
        await asyncio.sleep(0.02)
        assert turn_lock.locked() and not task.done() and not finished.is_set()

        async def next_turn():
            async with turn_lock:
                next_entered.set()

        following = asyncio.create_task(next_turn())
        await asyncio.sleep(0.02)
        assert not next_entered.is_set(), "next owner input cannot dispatch during native write"
        assert not any(
            candidate.get_name() == "owner-native-compaction-commit"
            for candidate in asyncio.all_tasks()
        ), "A cancellable named inner Task must not own the worker"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert finished.is_set() and not turn_lock.locked()
    await asyncio.wait_for(following, 2)
    assert next_entered.is_set()
