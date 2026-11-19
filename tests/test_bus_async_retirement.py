"""Accepted bus calls keep and retire their original async result during cancellation."""

import asyncio
import inspect
import threading
from contextlib import asynccontextmanager

import pytest

from agent_comms.execution.child_process import ChildRetirementFailed, ReadWork


class HeldFactory:
    """The original synchronous call stays inside its real worker until released."""

    def __init__(self, produce):
        self.produce = produce
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5), "test did not release its original factory"
        return self.produce()

    @asynccontextmanager
    async def running(self, bus):
        pending = asyncio.create_task(bus.when_open(self))
        try:
            assert await asyncio.to_thread(self.entered.wait, 5)
            yield pending
        finally:
            self.release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


class NativeAwaitable:
    """An Awaitable that is neither a native coroutine nor a Future."""

    def __init__(self):
        self.started = False

    def __await__(self):
        async def answer():
            self.started = True
            return 41
        return answer().__await__()


@pytest.mark.parametrize("kind", ("value", "coroutine", "future", "awaitable"))
async def test_original_bus_call_preserves_sync_and_async_answers(comms, kind):
    async def answer():
        return 41
    future = asyncio.get_running_loop().create_future()
    future.set_result(41)
    values = {"value": 41, "coroutine": answer, "future": future, "awaitable": NativeAwaitable()}
    value = values[kind]
    result = await comms.bus.when_open(answer if kind == "coroutine" else lambda: value)
    assert result == 41


@pytest.mark.parametrize("repeat", (False, True))
@pytest.mark.parametrize("kind", ("coroutine", "future"))
async def test_cancelled_factory_retires_its_returned_work_once(comms, kind, repeat):
    effects = []
    async def followup():
        effects.append("executed")
    value = followup() if kind == "coroutine" else asyncio.get_running_loop().create_future()
    factory = HeldFactory(lambda: value)
    try:
        async with factory.running(comms.bus) as pending:
            pending.cancel()
            await asyncio.sleep(0)
            if repeat:
                pending.cancel()
                await asyncio.sleep(0)
            assert not pending.done(), "the accepted factory must finish before cancellation escapes"
            factory.release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert factory.calls == 1
        assert effects == []
        if kind == "coroutine":
            assert inspect.getcoroutinestate(value) == inspect.CORO_CLOSED
        else:
            assert value.cancelled()
    finally:
        if kind == "coroutine":
            value.close()
        else:
            value.cancel()


async def test_returned_running_task_finishes_its_cleanup_despite_repeated_cancellation(comms):
    started, retiring, release, finished = (asyncio.Event() for _ in range(4))
    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            retiring.set()
            await release.wait()
            finished.set()
    original = asyncio.create_task(work())
    await started.wait()
    factory = HeldFactory(lambda: original)
    try:
        async with factory.running(comms.bus) as pending:
            pending.cancel()
            await asyncio.sleep(0)
            factory.release.set()
            await asyncio.wait_for(retiring.wait(), 2)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert finished.is_set() and original.cancelled() and factory.calls == 1
    finally:
        release.set()
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)


async def test_original_factory_failure_remains_the_cause_of_cancellation(comms):
    original = RuntimeError("original factory failed")
    def fail():
        raise original
    factory = HeldFactory(fail)
    async with factory.running(comms.bus) as pending:
        pending.cancel()
        await asyncio.sleep(0)
        factory.release.set()
        with pytest.raises(asyncio.CancelledError) as stopped:
            await pending
    assert stopped.value.__cause__ is original
    assert factory.calls == 1


async def test_failed_returned_retirement_is_not_hidden_by_cancellation(comms):
    original = ChildRetirementFailed("native cleanup acknowledgement failed")
    value = asyncio.get_running_loop().create_future()
    value.set_exception(original)
    factory = HeldFactory(lambda: value)
    async with factory.running(comms.bus) as pending:
        pending.cancel()
        await asyncio.sleep(0)
        factory.release.set()
        with pytest.raises(ChildRetirementFailed) as failed:
            await pending
    assert failed.value is original and factory.calls == 1


async def test_cancellation_after_async_start_uses_the_original_task(comms):
    started, finished = asyncio.Event(), asyncio.Event()
    calls = []
    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    def original():
        calls.append("factory")
        return work()
    pending = asyncio.create_task(comms.bus.when_open(original))
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finished.is_set() and calls == ["factory"]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_declared_read_still_drops_a_blocked_thread():
    entered, release = threading.Event(), threading.Event()
    def read():
        entered.set()
        assert release.wait(5)
    pending = asyncio.create_task(ReadWork.run_async(read))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, .25)
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
