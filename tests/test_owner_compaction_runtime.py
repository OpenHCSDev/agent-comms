"""Provider-free cancellation controls for the real owner worker lifetime."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from agent_comms import owner_compaction_runtime
from agent_comms.owner_compaction_prepare import NativePreparation, NativeWitness
from agent_comms.owner_compaction_provider import NativeSummary
from agent_comms.owner_compaction_runtime import _commit_native_summary
from agent_comms.private_path import FileRevision
from agent_comms.pi_vocabulary import ManualCompactionReason


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["owner", "inner_wrapper", "all_tasks"])
async def test_owner_lock_joins_underlying_worker_not_cancelled_asyncio_wrapper(
    monkeypatch, shutdown, tmp_path
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

    saved = tmp_path / 'saved.jsonl'
    saved.write_text('Original worker lifetime fixture\n')
    witness = NativeWitness("session-id", str(saved), "leaf", "kept", FileRevision.from_stat(saved.stat()))
    prepared = NativePreparation(
        witness,
        1, False,
    )
    class Bridge:
        def commit(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(timeout=4), "test release never arrived"
            finished.set()
            return SimpleNamespace(status="committed")

    class Persistent:
        @asynccontextmanager
        async def external_write(self, *_args):
            # This control cancels inside the joined OS worker, before reload.
            # Actual SDK reload/source custody is qualified separately.
            yield SimpleNamespace()

    async def owner():
        async with turn_lock:
            await _commit_native_summary(
                Bridge(), SimpleNamespace(), 1, Persistent(), prepared,
                SimpleNamespace(), NativeSummary("worker lifetime control, no provider", None, None),
                reason=ManualCompactionReason,
            )

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


@pytest.mark.asyncio
async def test_unresolved_native_write_has_no_completion_observation():
    from agent_comms.compaction_errors import CompactionJournalError
    from agent_comms.compaction_records import CompactionOperation
    from agent_comms.compaction_states import UnknownOperation

    operation = CompactionOperation("exact-commit", "saved-session", "{}", UnknownOperation(), None)

    async def writer(summary):
        return operation

    summary = NativeSummary("selected output", None, None)
    with pytest.raises(CompactionJournalError, match="reconcile exact ID"):
        await summary.commit_with(writer)
