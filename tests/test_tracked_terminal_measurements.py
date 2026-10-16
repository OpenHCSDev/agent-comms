"""Check original cleanup ordering and exception identity, without native/provider input."""

import asyncio
import time

import pytest

from agent_comms.native_startup import NativeStartupAdmission
from agent_comms.tracked_turn import TrackedTurnSession


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
async def test_acquisition_retirement_measurement_preserves_cleanup_and_failure(
    tmp_path, monkeypatch, outcome
):
    # The concrete risk is registration before acquisition: resources acquired
    # before a failure/cancel must remain inside the original stack's timing.
    turn = object.__new__(TrackedTurnSession)
    turn.startup = NativeStartupAdmission(tmp_path)
    original = RuntimeError("original acquisition failure")
    acquired = asyncio.Event()
    callbacks = []

    async def cleanup(name):
        callbacks.append((name, time.monotonic_ns()))
        await asyncio.sleep(0)

    async def acquire(self, transport, *, reuse):
        assert self is turn and reuse is True
        # Input transport has a shorter lifetime than the child. Register each
        # callback with its real owner, not all on the input transport stack.
        transport.push_async_callback(cleanup, "transport")
        self.resources.push_async_callback(cleanup, "first")
        self.resources.push_async_callback(cleanup, "last")
        acquired.set()
        if outcome == "failure":
            raise original
        await asyncio.Event().wait()

    monkeypatch.setattr(TrackedTurnSession, "acquire_native", acquire)
    task = asyncio.create_task(turn.complete())
    await asyncio.wait_for(acquired.wait(), 1)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError) as caught:
            await task
        assert caught.value is original

    assert [name for name, _ in callbacks] == ["transport", "last", "first"]
    operations = turn.startup.measurements.operations
    assert set(operations) == {"pi_session_retirement"}
    retirement = operations["pi_session_retirement"]
    assert retirement.count == 1
    assert callbacks[0][1] <= retirement.maximum_started_ns <= callbacks[1][1]
    assert retirement.maximum_finished_ns >= callbacks[-1][1]
    assert retirement.total_ns == retirement.maximum_ns > 0
