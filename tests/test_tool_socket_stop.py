"""An interrupted selected turn retires its socket before child ownership can block."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, nullcontext
from types import MethodType, SimpleNamespace

import pytest

from agent_comms.selected_tool_broker import SelectedToolMode
from agent_comms.tracked_turn import NoObserver, TrackedTurnSession
from conftest import await_event_or_task


class _Measurements:
    @staticmethod
    def operation(_name):
        return nullcontext()


@asynccontextmanager
async def _failures():
    yield


@pytest.mark.asyncio
async def test_cancelled_turn_unlinks_tool_socket_before_native_retirement(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    turn = object.__new__(TrackedTurnSession)
    turn.startup = SimpleNamespace(measurements=_Measurements())
    turn.selected_tool_mode = SelectedToolMode(lambda _request: None)
    turn.launch = SimpleNamespace(session=SimpleNamespace(directory=sessions), env={})
    turn.watchdog = SimpleNamespace(reading=lambda: None)
    turn.observe_event = NoObserver()
    turn.finished = False

    native = SimpleNamespace(
        failures=_failures,
        reader=SimpleNamespace(pending=SimpleNamespace(cancel_all=lambda: None)),
        proc=SimpleNamespace(pid=os.getpid()),
    )

    async def acquire(self, resources, *, reuse):
        assert reuse is True
        await TrackedTurnSession.open_transport(self, resources)
        return native

    running = asyncio.Event()

    async def attest(self):
        running.set()
        await asyncio.Future()

    retirement_started = asyncio.Event()
    finish_retirement = asyncio.Event()

    async def retain_or_close(self):
        retirement_started.set()
        await finish_retirement.wait()

    turn.acquire_native = MethodType(acquire, turn)
    turn.attest = MethodType(attest, turn)
    turn.retain_or_close = MethodType(retain_or_close, turn)

    task = asyncio.create_task(turn.complete())
    async with asyncio.timeout(2):
        await await_event_or_task(running.wait(), task)
    socket_path = sessions / "s"
    assert socket_path.exists()

    task.cancel()
    async with asyncio.timeout(2):
        await await_event_or_task(retirement_started.wait(), task)
    try:
        assert not socket_path.exists()
    finally:
        finish_retirement.set()
    with pytest.raises(asyncio.CancelledError):
        await task
