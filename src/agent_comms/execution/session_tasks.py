"""The original producer and turn-task states shared by session lifetime owners."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from agent_comms.agent_backend import AgentBackend
from agent_comms.execution.child_process import join_retirement
from agent_comms.messaging.watched import ChangeSignal


class BackgroundTask:
    """An idle producer; running members own cancellation and joined retirement."""

    @property
    def busy(self) -> bool:
        return False

    def owns(self, task: asyncio.Task[object]) -> bool:
        return False

    def producers(self) -> tuple[asyncio.Task[object], ...]:
        return ()

    def cancel(self) -> None:
        pass

    async def stop(self) -> None:
        self.cancel()
        await join_retirement(asyncio.gather(*self.producers(), return_exceptions=True))

    def wake(self) -> None:
        pass


@dataclass(frozen=True)
class RunningBackgroundTask(BackgroundTask):
    task: asyncio.Task[object]

    @property
    def busy(self) -> bool:
        return not self.task.done() and not self.task.cancelling()

    def owns(self, task: asyncio.Task[object]) -> bool:
        return self.task is task

    def producers(self) -> tuple[asyncio.Task[object], ...]:
        return (self.task,)

    def cancel(self) -> None:
        self.task.cancel()


@dataclass(frozen=True)
class WatchingObservationTask(RunningBackgroundTask):
    watcher: ChangeSignal

    def wake(self) -> None:
        self.watcher.wake()


class TurnTask(BackgroundTask):
    async def prepare_cancel(self, transition: Callable[[], Awaitable[None]]) -> None:
        pass

    async def abort(self, backend: AgentBackend) -> None:
        pass


@dataclass(frozen=True)
class RunningTurnTask(RunningBackgroundTask, TurnTask):

    @property
    def busy(self) -> bool:
        return True

    async def prepare_cancel(self, transition: Callable[[], Awaitable[None]]) -> None:
        await transition()

    async def abort(self, backend: AgentBackend) -> None:
        await backend.abort(self.task)
